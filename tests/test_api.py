"""Test dell'API (§9.1) su Postgres reale, con trasporto LLM e gateway finti."""

from __future__ import annotations

import httpx
import pytest_asyncio
from sqlalchemy import text

from ideai.adapters.llm.transport import RawEmbedding
from ideai.api.app import create_app

AUTH = {"Authorization": "Bearer test-api-key"}
DIM = 1024


def vec_str(values: list[float]) -> str:
    padded = values + [0.0] * (DIM - len(values))
    return "[" + ",".join(repr(float(v)) for v in padded) + "]"


class FakeTransport:
    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.embed_calls = 0

    async def available_models(self) -> list[str]:
        return ["qwen3:8b", "bge-m3:567m"]

    async def embed(self, *, model: str, texts: list[str]) -> RawEmbedding:
        self.embed_calls += 1
        return RawEmbedding(
            vectors=[[1.0] + [0.0] * (self.dim - 1) for _ in texts],
            tokens_in=0,
            latency_ms=0,
        )


class _FakeRole:
    def __init__(self, transport: FakeTransport) -> None:
        self._transport = transport

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        raw = await self._transport.embed(model=model, texts=texts)
        return raw.vectors


class FakeGateway:
    def __init__(self, transport: FakeTransport) -> None:
        self._transport = transport

    def embedder(self) -> _FakeRole:
        return _FakeRole(self._transport)


@pytest_asyncio.fixture
async def transport() -> FakeTransport:
    return FakeTransport()


@pytest_asyncio.fixture
async def client(session_factory, settings, transport):
    app = create_app(settings)
    app.state.session_factory = session_factory
    app.state.transports = {"local": transport}
    app.state.llm = FakeGateway(transport)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def seed_source(session, *, kind: str = "reddit", name: str = "reddit") -> int:
    return await session.scalar(
        text(
            "INSERT INTO sources (kind, name) VALUES (:kind, :name) "
            "ON CONFLICT (kind, name) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ),
        {"kind": kind, "name": name},
    )


async def seed_idea(
    session,
    *,
    source_id: int,
    title: str,
    summary: str = "sommario canonico",
    score: int | None = 50,
    status: str = "analyzed",
    verdict: str | None = "promising",
    category: str = "pain_point",
    tags: list[str] | None = None,
    source_kinds: list[str] | None = None,
    with_analysis: bool = True,
    external_id: str | None = None,
) -> tuple[int, int]:
    idea_id = await session.scalar(
        text(
            """
INSERT INTO idea_clusters (title, canonical_summary, status, opportunity_score,
                           item_count, source_kinds)
VALUES (:title, :summary, :status, :score, 1, CAST(:source_kinds AS text[]))
RETURNING id
"""
        ),
        {
            "title": title,
            "summary": summary,
            "status": status,
            "score": score,
            "source_kinds": source_kinds or ["reddit"],
        },
    )
    item_id = await session.scalar(
        text(
            """
INSERT INTO items (source_id, external_id, kind, thread_external_id, title, body,
                   content_hash, state, created_at, category, tags, idea_id)
VALUES (:source_id, :external_id, 'post', :external_id, :title, :body,
        :content_hash, 'analyzed', now(), :category, CAST(:tags AS text[]), :idea_id)
RETURNING id
"""
        ),
        {
            "source_id": source_id,
            "external_id": external_id or f"t3_{idea_id}",
            "title": title,
            "body": summary,
            "content_hash": f"hash-{idea_id}",
            "category": category,
            "tags": tags or ["pmi"],
            "idea_id": idea_id,
        },
    )
    await session.execute(
        text("INSERT INTO idea_items (idea_id, item_id, role) VALUES (:idea_id, :item_id, 'seed')"),
        {"idea_id": idea_id, "item_id": item_id},
    )
    if with_analysis:
        await session.execute(
            text(
                """
INSERT INTO analyses (idea_id, revision, model_spec, provider, prompt_version, schema_version,
                      payload, feasibility_score, economics_score, competition_score,
                      verdict, confidence, opportunity_score)
VALUES (:idea_id, 1, 'local/qwen3:8b', 'ollama_native', 'v1', 'v1',
        CAST('{"problem": "x"}' AS jsonb), 4, 3, 2, :verdict, 0.7, :score)
"""
            ),
            {"idea_id": idea_id, "verdict": verdict, "score": score},
        )
    return idea_id, item_id


async def seed_embedding(session, *, owner_kind: str, owner_id: int, values: list[float]) -> None:
    await session.execute(
        text(
            """
INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec)
VALUES (:owner_kind, :owner_id, 'local/bge-m3:567m', :dim, CAST(:vec AS vector))
ON CONFLICT (owner_kind, owner_id, model) DO UPDATE SET vec = EXCLUDED.vec
"""
        ),
        {
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "dim": DIM,
            "vec": vec_str(values),
        },
    )


# --------------------------------------------------------------------------- #
# Autenticazione e salute
# --------------------------------------------------------------------------- #
async def test_healthz_open_and_readyz_open(client):
    assert (await client.get("/api/v1/healthz")).status_code == 200
    ready = await client.get("/api/v1/readyz")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ok"


async def test_every_other_endpoint_requires_bearer(client):
    for path in ("/api/v1/ideas", "/api/v1/sources", "/api/v1/jobs", "/api/v1/stats", "/metrics"):
        response = await client.get(path)
        assert response.status_code == 401, path
        assert response.json()["detail"] == "API key mancante o non valida"
    bad = await client.get("/api/v1/ideas", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401


async def test_readyz_503_when_embed_dim_mismatch(session_factory, settings):
    wrong = FakeTransport(dim=512)
    app = create_app(settings)
    app.state.session_factory = session_factory
    app.state.transports = {"local": wrong}
    app.state.llm = FakeGateway(wrong)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.get("/api/v1/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert any("IDEAI_EMBED_DIM" in item for item in body["missing"])


# --------------------------------------------------------------------------- #
# /ideas
# --------------------------------------------------------------------------- #
async def test_ideas_filters_and_sort_and_total(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        await seed_idea(session, source_id=source_id, title="Bassa", score=10)
        await seed_idea(session, source_id=source_id, title="Media", score=80)
        await seed_idea(session, source_id=source_id, title="Alta", score=95)
        await session.commit()

    response = await client.get(
        "/api/v1/ideas",
        params={"min_score": 70, "sort": "opportunity_score", "limit": 2},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert [i["title"] for i in body["items"]] == ["Alta", "Media"]
    assert body["items"][0]["watch"] == {
        "enabled": False,
        "mode": "thread_full",
        "interval_s": 3600,
    }
    assert body["items"][0]["tags"] == ["pmi"]
    assert body["items"][0]["source_kinds"] == ["reddit"]

    filtered = await client.get(
        "/api/v1/ideas",
        params={"tag": "pmi", "verdict": "promising", "source_kind": "reddit"},
        headers=AUTH,
    )
    assert filtered.json()["total"] == 3
    empty = await client.get("/api/v1/ideas", params={"tag": "inesistente"}, headers=AUTH)
    assert empty.json()["total"] == 0


async def test_idea_detail_includes_analysis_items_and_timeline(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id, item_id = await seed_idea(session, source_id=source_id, title="Dettaglio")
        await session.execute(
            text(
                "INSERT INTO idea_updates (idea_id, item_id, kind, summary) "
                "VALUES (:idea_id, :item_id, 'new_comment', '2 nuovi commenti')"
            ),
            {"idea_id": idea_id, "item_id": item_id},
        )
        await session.commit()

    response = await client.get(f"/api/v1/ideas/{idea_id}", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["idea"]["id"] == idea_id
    assert body["current_analysis"]["verdict"] == "promising"
    assert len(body["revisions"]) == 1
    assert body["items"][0]["role"] == "seed"
    assert body["timeline"][0]["kind"] == "new_comment"

    assert (await client.get("/api/v1/ideas/999999", headers=AUTH)).status_code == 404


async def test_ideas_search_hybrid_returns_rrf_order(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        both_id, _ = await seed_idea(
            session, source_id=source_id, title="fatturazione automatica", score=90
        )
        fts_only_id, _ = await seed_idea(
            session, source_id=source_id, title="fatturazione manuale", score=40
        )
        vec_only_id, _ = await seed_idea(
            session, source_id=source_id, title="gestione magazzino", score=70
        )
        await seed_embedding(session, owner_kind="idea", owner_id=both_id, values=[1.0])
        await seed_embedding(session, owner_kind="idea", owner_id=vec_only_id, values=[0.7, 0.7])
        await seed_embedding(session, owner_kind="idea", owner_id=fts_only_id, values=[0.0, 1.0])
        await session.commit()

    response = await client.get("/api/v1/ideas/search", params={"q": "fatturazione"}, headers=AUTH)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items[0]["id"] == both_id, items
    assert {items[1]["id"], items[2]["id"]} == {fts_only_id, vec_only_id}


# --------------------------------------------------------------------------- #
# Watch, merge, delete, reanalyze
# --------------------------------------------------------------------------- #
async def test_watch_toggle_upserts_and_updates_idea_status(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id, _ = await seed_idea(session, source_id=source_id, title="Watch")
        await session.commit()

    enabled = await client.post(
        f"/api/v1/ideas/{idea_id}/watch",
        json={"enabled": True, "mode": "comments_only", "interval_s": 600},
        headers=AUTH,
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["status"] == "watching"
    assert enabled.json()["watch"] == {"enabled": True, "mode": "comments_only", "interval_s": 600}

    again = await client.post(
        f"/api/v1/ideas/{idea_id}/watch", json={"enabled": True, "interval_s": 1200}, headers=AUTH
    )
    assert again.json()["watch"]["interval_s"] == 1200
    async with session_factory() as session:
        added_by = await session.scalar(
            text("SELECT added_by FROM watchlist WHERE idea_id = :id"), {"id": idea_id}
        )
        count = await session.scalar(text("SELECT count(*) FROM watchlist"))
    assert added_by == "manual" and count == 1

    disabled = await client.post(
        f"/api/v1/ideas/{idea_id}/watch", json={"enabled": False}, headers=AUTH
    )
    assert disabled.json()["status"] == "analyzed"
    assert (
        await client.post("/api/v1/ideas/999999/watch", json={"enabled": True}, headers=AUTH)
    ).status_code == 404


async def test_merge_moves_items_and_archives_source(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        source_idea, item_id = await seed_idea(session, source_id=source_id, title="Da fondere")
        into_idea, _ = await seed_idea(session, source_id=source_id, title="Destinazione")
        await session.execute(
            text("INSERT INTO watchlist (idea_id, enabled) VALUES (:id, true)"),
            {"id": source_idea},
        )
        await session.commit()

    response = await client.post(
        f"/api/v1/ideas/{source_idea}/merge", json={"into_id": into_idea}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    async with session_factory() as session:
        status = await session.scalar(
            text("SELECT status FROM idea_clusters WHERE id = :id"), {"id": source_idea}
        )
        watch_enabled = await session.scalar(
            text("SELECT enabled FROM watchlist WHERE idea_id = :id"), {"id": source_idea}
        )
        moved = await session.scalar(
            text("SELECT idea_id FROM items WHERE id = :id"), {"id": item_id}
        )
        roles = (
            (
                await session.execute(
                    text("SELECT role FROM idea_items WHERE idea_id = :id"), {"id": into_idea}
                )
            )
            .scalars()
            .all()
        )
        merged_update = await session.scalar(
            text("SELECT count(*) FROM idea_updates WHERE idea_id = :id AND kind = 'merged'"),
            {"id": into_idea},
        )
    assert status == "archived"
    assert watch_enabled is False
    assert moved == into_idea
    assert len(roles) == 2
    assert merged_update == 1

    same = await client.post(
        f"/api/v1/ideas/{into_idea}/merge", json={"into_id": into_idea}, headers=AUTH
    )
    assert same.status_code == 422
    missing = await client.post(
        f"/api/v1/ideas/{into_idea}/merge", json={"into_id": 999999}, headers=AUTH
    )
    assert missing.status_code == 404


async def test_delete_removes_embeddings_items_and_idea(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        doomed, shared_item = await seed_idea(session, source_id=source_id, title="Da cancellare")
        keeper, keeper_item = await seed_idea(session, source_id=source_id, title="Sopravvive")
        # item condiviso fra le due idee
        await session.execute(
            text(
                "INSERT INTO idea_items (idea_id, item_id, role) VALUES (:idea, :item, 'evidence')"
            ),
            {"idea": keeper, "item": shared_item},
        )
        await seed_embedding(session, owner_kind="idea", owner_id=doomed, values=[1.0])
        await seed_embedding(session, owner_kind="item", owner_id=shared_item, values=[1.0])
        await seed_embedding(session, owner_kind="item", owner_id=keeper_item, values=[1.0])
        await session.commit()

    response = await client.delete(f"/api/v1/ideas/{doomed}", headers=AUTH)
    assert response.status_code == 204
    async with session_factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM idea_clusters WHERE id = :id"), {"id": doomed}
            )
            == 0
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM embeddings WHERE owner_kind = 'idea' AND owner_id = :id"
                ),
                {"id": doomed},
            )
            == 0
        )
        # l'item condiviso sopravvive (appartiene ancora a keeper)
        assert (
            await session.scalar(
                text("SELECT count(*) FROM items WHERE id = :id"), {"id": shared_item}
            )
            == 1
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM items WHERE id = :id"), {"id": keeper_item}
            )
            == 1
        )
    assert (await client.delete("/api/v1/ideas/999999", headers=AUTH)).status_code == 404


async def test_reanalyze_enqueues_job_with_dedup(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id, _ = await seed_idea(session, source_id=source_id, title="Ri-analisi")
        await session.commit()

    first = await client.post(f"/api/v1/ideas/{idea_id}/reanalyze", headers=AUTH)
    second = await client.post(f"/api/v1/ideas/{idea_id}/reanalyze", headers=AUTH)
    assert first.status_code == 200, first.text
    assert first.json()["job_id"] == second.json()["job_id"]
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT topic, payload, priority, dedup_key, state FROM jobs WHERE id = :id"),
                {"id": first.json()["job_id"]},
            )
        ).first()
    assert row._mapping["topic"] == "pipeline.analyze"
    assert row._mapping["payload"] == {"idea_id": idea_id, "reason": "manual"}
    assert row._mapping["priority"] == 50
    assert row._mapping["dedup_key"] == f"analyze:{idea_id}"
    assert row._mapping["state"] == "pending"
    assert (await client.post("/api/v1/ideas/999999/reanalyze", headers=AUTH)).status_code == 404


# --------------------------------------------------------------------------- #
# Sorgenti, target e job
# --------------------------------------------------------------------------- #
async def test_sources_targets_and_patch(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        await session.commit()

    created = await client.post(
        f"/api/v1/sources/{source_id}/targets",
        json={"target_ref": "r/ItaliaPersonalFinance", "target_kind": "subreddit"},
        headers=AUTH,
    )
    assert created.status_code == 201, created.text
    target_id = created.json()["id"]
    assert created.json()["enabled"] is True and created.json()["error_count"] == 0

    conflict = await client.post(
        f"/api/v1/sources/{source_id}/targets",
        json={"target_ref": "r/ItaliaPersonalFinance", "target_kind": "subreddit"},
        headers=AUTH,
    )
    assert conflict.status_code == 409
    invalid = await client.post(
        f"/api/v1/sources/{source_id}/targets",
        json={"target_ref": "x", "target_kind": "boh"},
        headers=AUTH,
    )
    assert invalid.status_code == 422
    missing_source = await client.post(
        "/api/v1/sources/999999/targets",
        json={"target_ref": "x", "target_kind": "subreddit"},
        headers=AUTH,
    )
    assert missing_source.status_code == 404

    listing = await client.get("/api/v1/sources", headers=AUTH)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body[0]["targets"][0]["target_ref"] == "r/ItaliaPersonalFinance"
    assert body[0]["rate_limit"]["bucket"] == "reddit:default"
    assert body[0]["rate_limit"]["limit"] == 100
    assert body[0]["calls_today"] == {"calls": 0, "cost_usd": 0.0}

    patched = await client.patch(
        f"/api/v1/targets/{target_id}",
        json={"enabled": False, "poll_interval_s": 900},
        headers=AUTH,
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False and patched.json()["poll_interval_s"] == 900
    assert (
        await client.patch("/api/v1/targets/999999", json={"enabled": True}, headers=AUTH)
    ).status_code == 404


async def test_jobs_listing_and_dead_job_retry(client, session_factory):
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO jobs (topic, state, attempts, last_error) "
                "VALUES ('pipeline.analyze', 'dead', 5, 'boom')"
            )
        )
        await session.execute(
            text("INSERT INTO jobs (topic, state) VALUES ('pipeline.scrape', 'pending')")
        )
        dead_id = await session.scalar(text("SELECT id FROM jobs WHERE state = 'dead'"))
        pending_id = await session.scalar(text("SELECT id FROM jobs WHERE state = 'pending'"))
        await session.commit()

    listing = await client.get("/api/v1/jobs", params={"state": "dead"}, headers=AUTH)
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    assert listing.json()["oldest_pending"] is not None

    retried = await client.post(f"/api/v1/jobs/{dead_id}/retry", headers=AUTH)
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "pending"
    assert retried.json()["attempts"] == 0
    assert retried.json()["last_error"] is None

    not_dead = await client.post(f"/api/v1/jobs/{pending_id}/retry", headers=AUTH)
    assert not_dead.status_code == 422
    assert (await client.post("/api/v1/jobs/999999/retry", headers=AUTH)).status_code == 404


async def test_stats_returns_kpi_and_queue(client, session_factory):
    async with session_factory() as session:
        source_id = await seed_source(session)
        await seed_idea(session, source_id=source_id, title="Watching", status="watching")
        await seed_idea(session, source_id=source_id, title="Nuova", status="new", score=None)
        await session.execute(
            text(
                "INSERT INTO llm_calls (role, provider, model, prompt_hash, schema_version, "
                "tokens_in, tokens_out, cost_usd, latency_ms, status) "
                "VALUES ('analyst','deepseek','deepseek-chat','h','v1',10,20,0.001,5,'ok')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO source_calls_daily (day, source_id, calls, cost_usd) "
                "VALUES ((now() AT TIME ZONE 'UTC')::date, :source_id, 3, 0.0007)"
            ),
            {"source_id": source_id},
        )
        await session.commit()

    response = await client.get("/api/v1/stats", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ideas"]["total"] == 2
    assert body["ideas"]["watching"] == 1
    assert body["analyses"] == 2
    assert body["watch_active"] == 0
    assert body["llm_cost_7d"][0]["role"] == "analyst"
    assert body["source_cost_7d"][0]["calls"] == 3
    assert body["queue"] == []
    assert body["failed_llm_calls"] == []
