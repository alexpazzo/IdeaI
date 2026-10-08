"""Spike hardware e compliance dello schema di triage (Fase 0, verifica V2).

Legge un subreddit reale tramite l'adapter ``reddit``, persiste gli item con
l'upsert di §5.2 e ne classifica ``--classify`` con il modello del ruolo ``triage``
attraverso il gateway. Produce ``tests/reports/spike_hardware.json`` con:

* ``tokens_per_second`` **misurato** da ``eval_count``/``eval_duration`` di Ollama.
  La misura richiede una chiamata diagnostica diretta a ``POST {base_url}/api/chat``
  del backend ``ollama_native`` (fuori dal gateway, che non espone i tempi di
  decoding); è dichiarata come diagnostica e non contabilizzata in ``llm_calls``.
* ``schema_compliance`` = quota di ``llm_calls.status='ok'`` sulle classificazioni
  del run (criterio di accettazione: ≥ 0,95, §11.2).

``--dry-run`` usa un trasporto finto deterministico, non tocca la rete né il
database e non richiede credenziali Reddit: i valori di tok/s sono **simulati** e
il report lo dichiara. Serve a verificare il build dello spike offline.

La misura reale di tok/s e la compliance reale richiedono Ollama avviato con i
modelli ``local/qwen3:8b`` e ``local/bge-m3:567m`` e le credenziali Reddit: le
esegue il coordinatore in V2/V5.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text

from ideai.adapters.llm import build_transports
from ideai.adapters.llm.gateway import LlmGateway
from ideai.adapters.llm.transport import RawCompletion
from ideai.adapters.sources.reddit import RedditAdapter
from ideai.config import Settings, require_reddit_credentials, resolve_model_spec
from ideai.db.session import create_engine_and_session
from ideai.domain import ItemCategory, NormalizedItem, SourceTarget
from ideai.prompts.loader import load_prompt
from ideai.prompts.schemas import TriageBatch
from ideai.runtime.ratelimit import RateLimiter

DEFAULT_OUT = Path("tests/reports/spike_hardware.json")

#: Prompt diagnostico per la misura di tok/s (una frase, output corto).
_DIAGNOSTIC_PROMPT = "Spiega in una frase perché il cielo è blu."

#: Categorie cicliche usate dal trasporto finto in ``--dry-run``.
_DRY_CATEGORIES = [category.value for category in ItemCategory]

_UPSERT_ITEM_SQL = text(
    """
INSERT INTO items (
    source_id, external_id, kind, thread_external_id, parent_external_id,
    title, body, author_hash, url, score, num_comments, lang,
    created_at, edited_at, content_hash, raw, state
) VALUES (
    :source_id, :external_id, :kind, :thread_external_id, :parent_external_id,
    :title, :body, :author_hash, :url, :score, :num_comments, :lang,
    :created_at, :edited_at, :content_hash, CAST(:raw AS jsonb), 'new'
)
ON CONFLICT (source_id, external_id) DO UPDATE SET
    score = EXCLUDED.score,
    num_comments = EXCLUDED.num_comments,
    edited_at = EXCLUDED.edited_at,
    fetched_at = now(),
    url = EXCLUDED.url,
    body = CASE WHEN items.content_hash <> EXCLUDED.content_hash
                THEN EXCLUDED.body ELSE items.body END,
    content_hash = CASE WHEN items.content_hash <> EXCLUDED.content_hash
                        THEN EXCLUDED.content_hash ELSE items.content_hash END,
    raw = CASE WHEN items.state IN ('candidate', 'analyzed')
               THEN items.raw ELSE EXCLUDED.raw END
RETURNING id
"""
)

_UPSERT_SOURCE_SQL = text(
    """
INSERT INTO sources (kind, name, config)
VALUES ('reddit', 'reddit', CAST(:config AS jsonb))
ON CONFLICT (kind, name) DO UPDATE SET config = sources.config
RETURNING id
"""
)


def _format_batch(texts: list[str]) -> str:
    """Formatta il lotto per ``{batch}`` del prompt di triage (indice + testo)."""
    blocks = [f"### Item {index}\n{body}" for index, body in enumerate(texts)]
    return "\n\n".join(blocks)


def _item_text(item: NormalizedItem) -> str:
    title = (item.title or "").strip()
    return f"{title}\n\n{item.body}" if title else item.body


class _DryRunTransport:
    """Trasporto finto deterministico: nessuna rete, risposte JSON conformi."""

    name = "dry-run"

    def __init__(self) -> None:
        self.chat_calls = 0
        self.tokens_out = 0
        self.latency_ms = 0

    async def chat_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        temperature: float,
    ) -> RawCompletion:
        self.chat_calls += 1
        count = max(1, user.count("### Item"))
        results = [
            {
                "index": index,
                "confidence": 0.9,
                "category": _DRY_CATEGORIES[index % len(_DRY_CATEGORIES)],
                "one_line_summary": f"Riepilogo sintetico {index}",
                "problem": None,
                "audience": None,
                "tags": [],
                "dup_of_idea_id": None,
            }
            for index in range(count)
        ]
        tokens_out = 40 * count
        latency_ms = 1000 * count
        self.tokens_out += tokens_out
        self.latency_ms += latency_ms
        return RawCompletion(
            content=json.dumps({"results": results}),
            tokens_in=64,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
        )

    async def embed(self, *, model: str, texts: list[str]) -> object:
        raise NotImplementedError("dry-run: nessun embedding")


def _dry_run_payloads(count: int) -> list[dict]:
    """Payload sintetici costruiti dalle fixture Reddit registrate (pura normalizzazione)."""
    fixtures_dir = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "reddit"
    payloads: list[dict] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        payloads.append(json.loads(path.read_text(encoding="utf-8")))
    if not payloads:
        raise SystemExit(f"nessuna fixture Reddit in {fixtures_dir}")
    return [payloads[i % len(payloads)] for i in range(count)]


async def _measure_tokens_per_second(
    settings: Settings, model_spec: str
) -> tuple[float | None, dict]:
    """Misura diagnostica di tok/s su Ollama (``eval_count``/``eval_duration``)."""
    backend_name, model = resolve_model_spec(model_spec, settings.llm_backends)
    backend = settings.llm_backends[backend_name]
    if backend.type != "ollama_native":
        return None, {
            "measured": False,
            "reason": f"backend {backend.type}: eval_count/eval_duration non disponibili",
        }
    url = backend.base_url.rstrip("/") + "/api/chat"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _DIAGNOSTIC_PROMPT}],
        "stream": False,
        "think": False,
        "options": {"num_predict": 64},
    }
    async with httpx.AsyncClient(timeout=backend.timeout_s) as client:
        response = await client.post(url, json=body)
        response.raise_for_status()
        data = response.json()
    eval_count = int(data.get("eval_count") or 0)
    eval_duration_ns = int(data.get("eval_duration") or 0)
    tps = eval_count / (eval_duration_ns / 1e9) if eval_duration_ns > 0 else None
    return tps, {
        "measured": True,
        "diagnostic": "POST /api/chat diretto (fuori dal gateway)",
        "eval_count": eval_count,
        "eval_duration_ns": eval_duration_ns,
        "prompt_eval_count": int(data.get("prompt_eval_count") or 0),
    }


async def _classify(
    settings: Settings, session_factory, gateway: LlmGateway, items: list[NormalizedItem]
) -> tuple[dict[int, str], int]:
    """Classifica gli item con il gateway; ritorna ``{indice: categoria}`` e i fallimenti."""
    prompt = load_prompt("triage", "v1")
    provider = gateway.triage()
    categories: dict[int, str] = {}
    failures = 0
    batch = max(1, settings.triage_batch)
    for start in range(0, len(items), batch):
        chunk = items[start : start + batch]
        user = prompt.render_user(batch=_format_batch([_item_text(i) for i in chunk]))
        try:
            result = await provider.complete_json(
                model=provider.model_spec,
                system=prompt.system,
                user=user,
                schema=TriageBatch.model_json_schema(),
                max_tokens=prompt.max_tokens,
                temperature=prompt.temperature,
            )
            parsed = TriageBatch.model_validate(result.content)
        except Exception:  # noqa: BLE001 - un lotto fallito non interrompe lo spike
            failures += 1
            continue
        for triage in parsed.results:
            if 0 <= triage.index < len(chunk):
                categories[start + triage.index] = triage.category.value
    return categories, failures


def _items_from_fixtures(adapter, fixtures: Path, count: int) -> list:
    """Normalizza le fixture registrate con l'adapter reale, senza toccare la rete.

    I payload sono resi distinti (id e corpo variano) così i ``content_hash`` non
    collidono e il cancello G0 degli esperimenti non scarta le varianti.
    """
    files = sorted(fixtures.glob("*.json"))
    if not files:
        raise SystemExit(f"nessuna fixture in {fixtures}")
    items = []
    for index in range(count):
        raw = json.loads(files[index % len(files)].read_text(encoding="utf-8"))
        payload = dict(raw)
        payload["id"] = f"spike{index:04d}"
        name = str(raw.get("name") or "t3_spike")
        payload["name"] = f"{name[:3]}{index:04d}"
        for key in ("selftext", "body"):
            if isinstance(payload.get(key), str):
                payload[key] = payload[key] + f"\n\n(variante spike #{index})"
        items.append(adapter.normalize(payload))
    return items


async def _run_real(args: argparse.Namespace) -> dict:
    settings = Settings()
    engine, session_factory = create_engine_and_session(settings)
    started_at = datetime.now(UTC)
    adapter = RedditAdapter(settings)
    try:
        if args.fixtures is not None:
            # Stessa pipeline della modalità live, ma con contenuto registrato: serve a
            # misurare tok/s e compliance senza dipendere dalle credenziali Reddit.
            items = _items_from_fixtures(adapter, args.fixtures, args.items)
            data_source = "fixtures"
        else:
            require_reddit_credentials(settings)
            target = SourceTarget(
                id=0,
                source_id=0,
                target_ref=args.subreddit,
                target_kind="subreddit",
                cursor=None,
            )
            page = await adapter.fetch_new(target, None, args.items)
            items = page.items[: args.items]
            data_source = "reddit"

        async with session_factory() as session:
            source_id = (
                await session.execute(
                    _UPSERT_SOURCE_SQL,
                    {
                        "config": json.dumps(
                            {
                                "rpm": settings.reddit_rpm,
                                "cost_per_call_usd": 0.0,
                                "engagement_saturation": 5000,
                            }
                        )
                    },
                )
            ).scalar_one()
            item_ids: list[int] = []
            for item in items:
                row = (
                    await session.execute(
                        _UPSERT_ITEM_SQL,
                        {
                            "source_id": source_id,
                            "external_id": item.external_id,
                            "kind": item.kind,
                            "thread_external_id": item.thread_external_id,
                            "parent_external_id": item.parent_external_id,
                            "title": item.title,
                            "body": item.body,
                            "author_hash": item.author_hash,
                            "url": item.url,
                            "score": item.score,
                            "num_comments": item.num_comments,
                            "lang": item.lang,
                            "created_at": item.created_at,
                            "edited_at": item.edited_at,
                            "content_hash": item.content_hash,
                            "raw": json.dumps(item.raw),
                        },
                    )
                ).first()
                item_ids.append(row.id)
            await session.commit()

        gateway = LlmGateway(
            settings, session_factory, build_transports(settings), RateLimiter(session_factory)
        )
        classified = items[: args.classify]
        categories, failures = await _classify(settings, session_factory, gateway, classified)

        async with session_factory() as session:
            for index, category in categories.items():
                await session.execute(
                    text(
                        "UPDATE items SET state='triaged', category=:category, "
                        "triage_confidence=:confidence, attempts=attempts+1 WHERE id=:id"
                    ),
                    {"category": category, "confidence": None, "id": item_ids[index]},
                )
            await session.commit()
            compliance_row = (
                await session.execute(
                    text(
                        "SELECT count(*) AS total, "
                        "count(*) FILTER (WHERE status='ok') AS ok "
                        "FROM llm_calls WHERE role='triage' AND created_at >= :started_at"
                    ),
                    {"started_at": started_at},
                )
            ).one()

        tps, tps_meta = await _measure_tokens_per_second(settings, settings.triage_model)
        total_calls = int(compliance_row.total)
        return {
            "data_source": data_source,
            "model": settings.triage_model,
            "tokens_per_second": round(tps, 2) if tps is not None else None,
            "tokens_per_second_simulated": False,
            "tokens_per_second_detail": tps_meta,
            "schema_compliance": (compliance_row.ok / total_calls) if total_calls else 0.0,
            "schema_compliance_source": "llm_calls.status (role='triage')",
            "classifications_attempted": len(classified),
            "classifications_failed": failures,
            "llm_calls_triage": total_calls,
            "items_read": len(items),
            "items_persisted": len(item_ids),
            "persisted": True,
        }
    finally:
        await engine.dispose()


async def _run_dry(args: argparse.Namespace) -> dict:
    os.environ.setdefault("IDEAI_API_KEY", "dry-run")
    os.environ.setdefault("IDEAI_SALT", "dry-run")
    settings = Settings()
    adapter = RedditAdapter(settings)
    payloads = _dry_run_payloads(args.items)
    items = [adapter.normalize(payload) for payload in payloads]

    transport = _DryRunTransport()
    prompt = load_prompt("triage", "v1")
    classified = items[: args.classify]
    categories: dict[int, str] = {}
    failures = 0
    batch = max(1, settings.triage_batch)
    for start in range(0, len(classified), batch):
        chunk = classified[start : start + batch]
        user = prompt.render_user(batch=_format_batch([_item_text(i) for i in chunk]))
        raw = await transport.chat_json(
            model=settings.triage_model,
            system=prompt.system,
            user=user,
            schema=TriageBatch.model_json_schema(),
            max_tokens=prompt.max_tokens,
            temperature=prompt.temperature,
        )
        try:
            parsed = TriageBatch.model_validate(json.loads(raw.content))
        except Exception:  # noqa: BLE001 - il trasporto finto risponde sempre conforme
            failures += 1
            continue
        for triage in parsed.results:
            categories[start + triage.index] = triage.category.value

    ok_calls = transport.chat_calls - failures
    simulated_tps = (
        round(transport.tokens_out / (transport.latency_ms / 1000), 2)
        if transport.latency_ms
        else None
    )
    return {
        "model": settings.triage_model,
        "tokens_per_second": simulated_tps,
        "tokens_per_second_simulated": True,
        "tokens_per_second_detail": {
            "measured": False,
            "reason": "dry-run: trasporto finto deterministico, nessun backend contattato",
        },
        "schema_compliance": (ok_calls / transport.chat_calls) if transport.chat_calls else 0.0,
        "schema_compliance_source": "trasporto finto (dry-run)",
        "classifications_attempted": len(classified),
        "classifications_failed": failures,
        "llm_calls_triage": transport.chat_calls,
        "items_read": len(items),
        "items_persisted": 0,
        "persisted": False,
    }


async def run(args: argparse.Namespace) -> dict:
    result = await (_run_dry(args) if args.dry_run else _run_real(args))
    source_label = getattr(args, "fixtures", None)
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "dry_run": args.dry_run,
        "subreddit": None if source_label else args.subreddit,
        "fields_read": [
            "items.state",
            "items.category",
            "items.triage_confidence",
            "llm_calls.status",
            "ollama eval_count/eval_duration (diagnostica diretta)",
        ],
        "notes": (
            "dry-run: tok/s simulato, nessuna rete né database; "
            "la misura reale richiede Ollama con local/qwen3:8b e local/bge-m3:567m"
            if args.dry_run
            else (
                f"misura reale su contenuto registrato ({source_label}): tok/s da "
                "eval_count/eval_duration di Ollama; schema_compliance da llm_calls.status "
                "delle classificazioni del run"
                if source_label
                else "misura reale su Reddit: tok/s da eval_count/eval_duration di Ollama; "
                "schema_compliance da llm_calls.status delle classificazioni del run"
            )
        ),
        **result,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Spike hardware e compliance di triage (V2).")
    parser.add_argument("--subreddit", default="ItaliaPersonalFinance")
    parser.add_argument("--items", type=int, default=100)
    parser.add_argument("--classify", type=int, default=20)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="usa le fixture registrate invece di leggere Reddit (nessuna credenziale)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="trasporto finto deterministico; nessuna rete, nessun DB, nessuna credenziale",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
