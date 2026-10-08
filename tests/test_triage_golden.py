"""Golden set di triage (§11.4a, verifica V5).

Legge ``tests/fixtures/triage_golden.jsonl`` (righe con ``text`` ed
``expected_category``), esegue il triage reale via gateway e misura due cose:

* ``schema_compliance`` = quota di ``llm_calls.status='ok'`` sulle classificazioni
  del run: **soglia bloccante** ≥ 0,95 (§11.2), altrimenti il test fallisce;
* ``category_accuracy`` = accuratezza rispetto alle etichette manuali: **registrata**
  nel report per ``prompt_version``, mai usata come soglia.

Il test è **saltato esplicitamente** se ``IDEAI_RUN_LLM_TESTS`` non vale ``1`` o se
la fixture è assente: la suite offline di ``uv run pytest`` non contatta modelli.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from ideai.adapters.llm import build_transports
from ideai.adapters.llm.gateway import LlmGateway
from ideai.domain import ItemCategory
from ideai.prompts import PROMPT_CURRENT
from ideai.prompts.loader import load_prompt
from ideai.prompts.schemas import TriageBatch
from ideai.runtime.ratelimit import RateLimiter

FIXTURE = Path(__file__).parent / "fixtures" / "triage_golden.jsonl"
REPORT = Path(__file__).parent / "reports" / "triage_golden_v1.json"

RUN_LLM_TESTS = os.environ.get("IDEAI_RUN_LLM_TESTS") == "1"

#: Soglia bloccante di compliance dello schema (§11.2).
SCHEMA_COMPLIANCE_THRESHOLD = 0.95

pytestmark = pytest.mark.skipif(
    not RUN_LLM_TESTS or not FIXTURE.exists(),
    reason=(f"golden set di triage: richiede IDEAI_RUN_LLM_TESTS=1 e la fixture {FIXTURE}"),
)


def _read_rows() -> list[dict]:
    rows: list[dict] = []
    with FIXTURE.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _format_batch(texts: list[str]) -> str:
    return "\n\n".join(f"### Item {index}\n{body}" for index, body in enumerate(texts))


async def test_triage_golden_schema_compliance_and_accuracy(session_factory, settings):
    rows = _read_rows()
    taxonomy = {category.value for category in ItemCategory}
    labeled = [row for row in rows if row.get("expected_category") in taxonomy]
    assert labeled, "la fixture non contiene righe con expected_category valida"

    prompt = load_prompt("triage", PROMPT_CURRENT["triage"])
    gateway = LlmGateway(
        settings,
        session_factory,
        build_transports(settings),
        RateLimiter(session_factory),
    )
    provider = gateway.triage()

    started_at = datetime.now(UTC)
    predicted: dict[int, str] = {}
    batch_size = max(1, settings.triage_batch)
    for start in range(0, len(labeled), batch_size):
        chunk = labeled[start : start + batch_size]
        user = prompt.render_user(batch=_format_batch([row["text"] for row in chunk]))
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
        except Exception:  # noqa: BLE001 - un lotto fallito abbassa accuracy e compliance
            continue
        for triage in parsed.results:
            if 0 <= triage.index < len(chunk):
                predicted[start + triage.index] = triage.category.value

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT count(*) AS total, count(*) FILTER (WHERE status='ok') AS ok "
                    "FROM llm_calls WHERE role='triage' AND created_at >= :started_at"
                ),
                {"started_at": started_at},
            )
        ).one()

    total_calls = int(row.total)
    schema_compliance = (row.ok / total_calls) if total_calls else 0.0
    correct = sum(
        1
        for index, expected in enumerate([r["expected_category"] for r in labeled])
        if predicted.get(index) == expected
    )
    category_accuracy = correct / len(labeled)

    report = {
        "prompt_version": prompt.version,
        "model": provider.model_spec,
        "n": len(labeled),
        "schema_compliance": round(schema_compliance, 4),
        "category_accuracy": round(category_accuracy, 4),
        "llm_calls_triage": total_calls,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    assert schema_compliance >= SCHEMA_COMPLIANCE_THRESHOLD, (
        f"schema_compliance {schema_compliance:.4f} < {SCHEMA_COMPLIANCE_THRESHOLD} "
        f"(report: {REPORT})"
    )
