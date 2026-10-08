"""Popola il catalogo dalle fixture registrate, senza rete verso le sorgenti.

Strumento di **sviluppo**: costruisce un adapter che riproduce le pagine registrate in
``tests/fixtures/reddit/`` (normalizzate dall'adapter reale) e le fa passare per il vero
handler di scrape. Serve a verificare dashboard e API quando Reddit non è raggiungibile.

Con ``--pipeline`` esegue in-process anche triage → cluster → analyze → score: quella
parte richiede il backend di inferenza attivo.

Uso:
    uv run python scripts/dev_seed.py --count 40
    uv run python scripts/dev_seed.py --count 10 --pipeline
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from ideai.adapters.queue_pg import PgQueue
from ideai.adapters.sources.registry import build_adapters
from ideai.config import Settings
from ideai.db.session import create_engine_and_session
from ideai.domain import FetchPage, JobTopic, SourceCapabilities, SourceTarget
from ideai.logging import configure_logging, get_logger

log = get_logger("ideai.dev_seed")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "reddit"
SOURCE_NAME = "dev-fixtures"
TARGET_REF = "dev/fixtures"


class ReplayAdapter:
    """Adapter di sola lettura che riproduce pagine registrate, tramite l'adapter reale."""

    kind = "reddit"

    def __init__(self, inner, payloads: list[dict]) -> None:
        self._inner = inner
        self._payloads = payloads

    def capabilities(self) -> SourceCapabilities:
        return self._inner.capabilities()

    def rate_policy(self):
        return self._inner.rate_policy()

    def normalize(self, payload: dict):
        return self._inner.normalize(payload)

    async def fetch_thread(self, thread_ref, since):
        """Riproduce il thread: il post come primo item, poi i commenti registrati.

        Il vero `fetch_thread` ritorna il post di testa (per il refresh delle metriche) e
        i commenti con `created_utc > since`; qui la fixture del post viene riconosciuta
        dal fullname e i commenti sono quelli disponibili, così il watch iniziale ha
        davvero un commento nuovo e accoda l'analisi.
        """
        thread_id = getattr(thread_ref, "external_id", thread_ref)
        head = next((p for p in self._payloads if str(p.get("name")) == str(thread_id)), None)
        comments = [p for p in self._payloads if isinstance(p.get("body"), str)]
        selected: list[dict] = []
        if head is not None:
            selected.append(head)
        if comments:
            selected.append(comments[0])
        return FetchPage(
            items=[self.normalize(payload) for payload in selected],
            next_cursor=None,
            exhausted=True,
        )

    async def fetch_new(self, target: SourceTarget, cursor, limit: int) -> FetchPage:
        offset = int((cursor or {}).get("offset", 0))
        chunk = self._payloads[offset : offset + limit]
        items = [self.normalize(payload) for payload in chunk]
        next_offset = offset + len(chunk)
        return FetchPage(
            items=items,
            next_cursor={"offset": next_offset},
            exhausted=next_offset >= len(self._payloads),
        )


def load_payloads(fixtures: Path, count: int) -> list[dict]:
    """Cicla i payload registrati rendendoli distinti (content_hash ≠) e con id unici."""
    files = sorted(p for p in fixtures.glob("*.json"))
    if not files:
        raise SystemExit(f"nessuna fixture in {fixtures}")
    payloads: list[dict] = []
    for index in range(count):
        raw = json.loads(files[index % len(files)].read_text(encoding="utf-8"))
        payload = dict(raw)
        payload["id"] = f"dev{index:04d}"
        payload["name"] = f"{raw.get('name', 't3_dev')[:3]}{index:04d}"
        suffix = f"\n\n(dati di sviluppo #{index})"
        for key in ("selftext", "body"):
            if isinstance(payload.get(key), str):
                payload[key] = payload[key] + suffix
        payload["permalink"] = f"/r/dev/comments/dev{index:04d}/"
        payloads.append(payload)
    return payloads


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="esegue anche triage/cluster/analyze/score (richiede il backend di inferenza)",
    )
    args = parser.parse_args()

    settings = Settings()
    configure_logging(settings)
    engine, session_factory = create_engine_and_session(settings)

    payloads = load_payloads(args.fixtures, args.count)

    adapters = build_adapters(settings)
    adapters["reddit"] = ReplayAdapter(adapters["reddit"], payloads)

    async with session_factory() as session:
        source_id = await session.scalar(
            text(
                """
INSERT INTO sources (kind, name, config)
VALUES ('reddit', :name, CAST(:config AS jsonb))
ON CONFLICT (kind, name) DO UPDATE SET config = EXCLUDED.config
RETURNING id
"""
            ),
            {"name": SOURCE_NAME, "config": json.dumps({"engagement_saturation": 5000})},
        )
        target_id = await session.scalar(
            text(
                """
INSERT INTO source_targets (source_id, target_ref, target_kind, enabled)
VALUES (:source_id, :target_ref, 'subreddit', true)
ON CONFLICT (source_id, target_ref) DO UPDATE SET enabled = true
RETURNING id
"""
            ),
            {"source_id": source_id, "target_ref": TARGET_REF},
        )
        await session.commit()

    from ideai.adapters.llm import build_transports
    from ideai.adapters.llm.gateway import LlmGateway
    from ideai.pipeline.context import HandlerContext
    from ideai.pipeline.registry import handler_for
    from ideai.runtime.ratelimit import RateLimiter

    queue = PgQueue(session_factory, settings)
    limiter = RateLimiter(session_factory)
    context = HandlerContext(
        settings=settings,
        db=session_factory,
        queue=queue,
        llm=LlmGateway(settings, session_factory, build_transports(settings), limiter),
        rate=limiter,
        adapters=adapters,
        now=lambda: datetime.now(UTC),
    )

    topics = [JobTopic.SCRAPE.value]
    if args.pipeline:
        topics += [
            JobTopic.TRIAGE.value,
            JobTopic.CLUSTER.value,
            JobTopic.WATCH.value,
            JobTopic.ANALYZE.value,
            JobTopic.SCORE.value,
        ]

    await queue.enqueue(JobTopic.SCRAPE, {"target_id": target_id})
    processed: list[str] = []
    try:
        for _ in range(200):
            job = await queue.claim(topics, "dev-seed", settings.job_lease_s)
            if job is None:
                break
            await handler_for(job.topic)(job, context)
            processed.append(job.topic)
    finally:
        await engine.dispose()

    async with session_factory() as session:
        counts = (
            await session.execute(
                text(
                    "SELECT state, count(*) AS n FROM items WHERE source_id = :source_id "
                    "GROUP BY state ORDER BY state"
                ),
                {"source_id": source_id},
            )
        ).all()
    log.info(
        "dev seed completato",
        source_kind="reddit",
        job_eseguiti=len(processed),
        item_per_stato={row._mapping["state"]: row._mapping["n"] for row in counts},
    )
    print(f"job eseguiti: {', '.join(processed) or 'nessuno'}")
    for row in counts:
        print(f"  items {row._mapping['state']}: {row._mapping['n']}")
    if not args.pipeline:
        print("suggerimento: --pipeline per triage/analisi (richiede il backend di inferenza)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
