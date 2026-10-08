"""Test del gateway LLM (M3): cache, budget, riparazione, trasporti (§7).

I test del gateway usano il doppio deterministico ``FakeTransport``; i test dei
trasporti concreti usano ``httpx.MockTransport``. I test che scrivono su
``llm_calls``/``llm_cache`` richiedono il Postgres di ``tests/conftest.py``.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from ideai.adapters.llm.anthropic import AnthropicTransport
from ideai.adapters.llm.gateway import LlmGateway
from ideai.adapters.llm.ollama_native import OllamaNativeTransport
from ideai.config import BackendConfig, PriceConfig, Settings, resolve_model_spec
from ideai.domain import BudgetExhausted, LlmInvalidJSON
from ideai.prompts.schemas import TriageBatch
from tests.support.llm_double import FakeTransport, NullLimiter

LOCAL_BACKEND = BackendConfig(
    type="ollama_native",
    base_url="http://127.0.0.1:11434",
    timeout_s=120,
    max_concurrency=2,
)


def _valid_triage(index: int = 0) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "index": index,
                    "confidence": 0.9,
                    "category": "product_idea",
                    "one_line_summary": "Un'idea",
                    "problem": None,
                    "audience": None,
                    "tags": ["saas"],
                    "dup_of_idea_id": None,
                }
            ]
        }
    )


@pytest.fixture
def make_settings(pg_url):
    def _make(**kwargs) -> Settings:
        kwargs.setdefault("database_url", pg_url)
        return Settings(**kwargs)

    return _make


async def _call(provider, settings, user="corpo dell'item"):
    return await provider.complete_json(
        model=provider.model_spec,
        system="sistema",
        user=user,
        schema=TriageBatch.model_json_schema(),
        max_tokens=100,
        temperature=0.0,
    )


# --------------------------------------------------------------------------- #
# Cache e contabilizzazione
# --------------------------------------------------------------------------- #
async def test_complete_json_records_llm_call_and_cache_hit(make_settings, session_factory):
    settings = make_settings()
    transport = FakeTransport([_valid_triage()])
    gateway = LlmGateway(settings, session_factory, {"local": transport}, NullLimiter())
    provider = gateway.triage()

    first = await _call(provider, settings)
    second = await _call(provider, settings)

    assert transport.chat_calls == 1
    assert first.cache_hit is False
    assert first.content["results"][0]["category"] == "product_idea"
    assert second.cache_hit is True
    assert second.cost_usd == 0.0

    async with session_factory() as session:
        rows = (
            await session.execute(text("SELECT status, cost_usd FROM llm_calls ORDER BY id"))
        ).all()
    assert len(rows) == 2
    assert [row[0] for row in rows] == ["ok", "ok"]
    assert all(Decimal(row[1]) == 0 for row in rows)


# --------------------------------------------------------------------------- #
# Riparazione
# --------------------------------------------------------------------------- #
async def test_invalid_json_repaired_after_one_error(make_settings, session_factory):
    settings = make_settings()
    transport = FakeTransport(['{"results": "non è una lista"}', _valid_triage()])
    gateway = LlmGateway(settings, session_factory, {"local": transport}, NullLimiter())

    result = await _call(gateway.triage(), settings)

    assert result.cache_hit is False
    assert transport.chat_calls == 2
    # la riparazione include la risposta precedente e l'errore di validazione
    assert "Risposta precedente" in transport.calls[1].user
    assert "Errore di validazione" in transport.calls[1].user
    async with session_factory() as session:
        rows = (await session.execute(text("SELECT status FROM llm_calls ORDER BY id"))).all()
    assert [row[0] for row in rows] == ["ok"]


async def test_invalid_json_after_two_repairs_marks_status_and_raises(
    make_settings, session_factory
):
    settings = make_settings()
    transport = FakeTransport(['{"results": 1}'] * 3)
    gateway = LlmGateway(settings, session_factory, {"local": transport}, NullLimiter())

    with pytest.raises(LlmInvalidJSON):
        await _call(gateway.triage(), settings)

    assert transport.chat_calls == 3
    async with session_factory() as session:
        rows = (
            await session.execute(text("SELECT status, error FROM llm_calls ORDER BY id"))
        ).all()
    assert len(rows) == 1
    assert rows[0][0] == "invalid_json"
    assert rows[0][1]


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
async def test_budget_exhausted_after_previous_cost(make_settings, session_factory):
    settings = make_settings(
        analyst_model="deepseek/deepseek-chat",
        llm_prices={
            "deepseek/deepseek-chat": PriceConfig(in_=Decimal("0.27"), out=Decimal("1.10"))
        },
        llm_daily_budget_usd=Decimal("1.00"),
    )
    transport = FakeTransport([_valid_triage()])
    gateway = LlmGateway(settings, session_factory, {"deepseek": transport}, NullLimiter())

    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO llm_calls (role, provider, model, prompt_hash, status, cost_usd) "
                "VALUES ('analyst', 'deepseek', 'deepseek-chat', 'h', 'ok', 2.0)"
            )
        )
        await session.commit()

    with pytest.raises(BudgetExhausted):
        await _call(gateway.analyst(), settings)

    assert transport.chat_calls == 0


async def test_local_zero_cost_not_blocked_by_budget(make_settings, session_factory):
    settings = make_settings(llm_daily_budget_usd=Decimal("0"))
    transport = FakeTransport([_valid_triage()])
    gateway = LlmGateway(settings, session_factory, {"local": transport}, NullLimiter())

    result = await _call(gateway.triage(), settings)

    assert result.cache_hit is False
    assert transport.chat_calls == 1


# --------------------------------------------------------------------------- #
# Trasporti concreti
# --------------------------------------------------------------------------- #
async def test_ollama_native_sends_format_schema_and_think_false():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {"content": '{"ok": true}'},
                "prompt_eval_count": 5,
                "eval_count": 2,
            },
        )

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:11434"
    )
    transport = OllamaNativeTransport(LOCAL_BACKEND, client=client)

    completion = await transport.chat_json(
        model="qwen3:8b", system="sys", user="usr", schema=schema, max_tokens=10, temperature=0.0
    )

    body = captured["body"]
    assert body["format"] == schema
    assert body["think"] is False
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.0, "num_predict": 10}
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert completion.content == '{"ok": true}'
    assert completion.tokens_in == 5
    assert completion.tokens_out == 2


async def test_anthropic_tool_schema_roundtrip():
    captured: dict = {}
    payload = {"results": []}
    schema = {"type": "object", "properties": {"results": {"type": "array"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "tool_use", "name": "emit_result", "input": payload}],
                "usage": {"input_tokens": 4, "output_tokens": 6},
            },
        )

    os.environ["TEST_ANTHROPIC_KEY"] = "secret"
    backend = BackendConfig(
        type="anthropic",
        base_url="https://api.anthropic.com",
        api_key_env="TEST_ANTHROPIC_KEY",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com"
    )
    transport = AnthropicTransport(backend, client=client)

    completion = await transport.chat_json(
        model="claude", system="sys", user="usr", schema=schema, max_tokens=20, temperature=0.1
    )

    assert json.loads(completion.content) == payload
    assert captured["body"]["tools"][0]["input_schema"] == schema
    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert captured["url"].endswith("/v1/messages")
    assert completion.tokens_in == 4
    assert completion.tokens_out == 6


def test_resolve_model_spec_rejects_unknown_backend(settings):
    with pytest.raises(ValueError):
        resolve_model_spec("sconosciuto/modello", settings.llm_backends)
    with pytest.raises(ValueError):
        resolve_model_spec("senza-slash", settings.llm_backends)
