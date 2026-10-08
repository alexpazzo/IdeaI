"""Costruzione del golden set di triage (§11.4a, verifica V5).

Due modalità:

* **Estrazione dal catalogo** (default): ``--subreddit X --limit 200`` seleziona gli
  item già ingeriti dalla sorgente ``reddit`` per quel target e scrive un JSONL con
  ``{"item_id", "text", "expected_category": null}``, da etichettare a mano.
* **Conversione di un file etichettato** (``--input <jsonl>``): verifica che ogni
  riga abbia ``expected_category`` appartenente alla tassonomia di §7.5 e che il
  numero di righe sia ~200, poi scrive il file canonico ``{text, expected_category}``
  consumato da ``tests/test_triage_golden.py``.

Lo script non chiama alcun modello: l'etichettatura è manuale, come da §11.4a.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import text

from ideai.config import Settings
from ideai.db.session import create_engine_and_session
from ideai.domain import ItemCategory

#: Numero di righe atteso dal golden set (§11.4a: 200 item).
EXPECTED_ROWS = 200

#: Fascia tollerata intorno a ``EXPECTED_ROWS`` prima di segnalare l'anomalia.
ROW_TOLERANCE = 50

#: Stati degli item ammessi come candidati per il golden set.
_ELIGIBLE_STATES = ("new", "triaged", "candidate", "analyzed")

_SELECT_SQL = text(
    """
SELECT i.id, i.title, i.body
  FROM items i
  JOIN sources s ON s.id = i.source_id
 WHERE s.kind = 'reddit'
   AND i.state = ANY(:states)
   AND EXISTS (
       SELECT 1 FROM source_targets t
        WHERE t.source_id = i.source_id
          AND t.target_ref = ANY(:refs)
   )
 ORDER BY i.id DESC
 LIMIT :limit
"""
)


def _subreddit_refs(subreddit: str) -> list[str]:
    """Accetta ``ItaliaPersonalFinance`` o ``r/ItaliaPersonalFinance`` e i suoi alias."""
    name = subreddit.removeprefix("/").removeprefix("r/")
    return [name, f"r/{name}"]


def _item_text(title: str | None, body: str) -> str:
    title = (title or "").strip()
    return f"{title}\n\n{body}" if title else body


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: JSON non valido: {exc}") from exc
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def convert_labeled(input_path: Path, out_path: Path) -> int:
    """Valida un JSONL etichettato e scrive il golden set canonico."""
    taxonomy = {category.value for category in ItemCategory}
    rows = _read_jsonl(input_path)
    if not rows:
        raise ValueError(f"{input_path}: file vuoto")

    problems: list[str] = []
    converted: list[dict] = []
    for lineno, row in enumerate(rows, start=1):
        expected = row.get("expected_category")
        if expected not in taxonomy:
            problems.append(f"  riga {lineno}: expected_category={expected!r} fuori tassonomia")
            continue
        text_value = row.get("text")
        if not isinstance(text_value, str) or not text_value.strip():
            problems.append(f"  riga {lineno}: campo 'text' mancante o vuoto")
            continue
        converted.append(
            {
                "item_id": row.get("item_id", lineno),
                "text": text_value,
                "expected_category": expected,
            }
        )

    if problems:
        taxonomy_hint = "\n".join(
            ["etichette non valide:", *problems, f"tassonomia §7.5: {sorted(taxonomy)}"]
        )
        raise ValueError(taxonomy_hint)

    if abs(len(converted) - EXPECTED_ROWS) > ROW_TOLERANCE:
        print(
            f"ATTENZIONE: il golden set ha {len(converted)} righe, "
            f"attese ~{EXPECTED_ROWS} (§11.4a)."
        )

    _write_jsonl(out_path, converted)
    print(f"golden set scritto: {out_path} ({len(converted)} righe)")
    return len(converted)


async def extract_from_db(subreddit: str, limit: int, out_path: Path) -> int:
    """Estrae gli item già ingeriti dal catalogo e scrive il JSONL da etichettare."""
    settings = Settings()
    engine, session_factory = create_engine_and_session(settings)
    try:
        async with session_factory() as session:
            result = await session.execute(
                _SELECT_SQL,
                {
                    "states": list(_ELIGIBLE_STATES),
                    "refs": _subreddit_refs(subreddit),
                    "limit": limit,
                },
            )
            rows = [
                {
                    "item_id": row.id,
                    "text": _item_text(row.title, row.body),
                    "expected_category": None,
                }
                for row in result
            ]
    finally:
        await engine.dispose()

    if not rows:
        raise SystemExit(
            f"nessun item trovato per il subreddit {subreddit!r}: "
            "esegui prima un ingest (V3) o usa --input."
        )

    _write_jsonl(out_path, rows)
    print(
        f"estratte {len(rows)} righe da etichettare: {out_path}\n"
        "Compila 'expected_category' con la tassonomia di §7.5 e poi converti con "
        f"--input {out_path}."
    )
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Costruisce il golden set di triage (§11.4a).")
    parser.add_argument("--subreddit", default="ItaliaPersonalFinance")
    parser.add_argument("--limit", type=int, default=EXPECTED_ROWS)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/fixtures/triage_golden_raw.jsonl"),
        help="file di output (default: il JSONL da etichettare)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="JSONL già etichettato da convertire nel golden set canonico",
    )
    args = parser.parse_args(argv)

    if args.input is not None:
        convert_labeled(args.input, args.out)
    else:
        asyncio.run(extract_from_db(args.subreddit, args.limit, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
