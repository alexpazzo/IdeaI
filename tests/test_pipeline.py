"""Test della pipeline M5 (§8): cancelli G0/G1/G2, analisi, scoring, watch, retention.

Postgres reale (``tests/conftest.py``), gateway LLM reale con trasporto finto
(``tests/support/llm_double.py``) e adapter sorgente finto: gli handler sono
invocati direttamente con il registry, senza worker.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from ideai.adapters.llm.gateway import LlmGateway
from ideai.adapters.queue_pg import PgQueue
from ideai.domain import (
    FetchPage,
    Job,
    NormalizedItem,
    RatePolicy,
    SourceCapabilities,
    content_hash,
)
from ideai.pipeline.context import HandlerContext
from ideai.pipeline.dedup import vec_literal
from ideai.pipeline.registry import handler_for
from tests.support.llm_double import FakeTransport, NullLimiter

DIM = 1024


# --------------------------------------------------------------------------- #
# Vettori controllati
# --------------------------------------------------------------------------- #
def _default_vec(text_value: str, dim: int = DIM) -> list[float]:
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        digest = hashlib.sha256(f"{text_value}:{counter}".encode()).digest()
        out.extend(byte / 255.0 for byte in digest)
        counter += 1
    return out[:dim]


def basis(index: int, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


def with_cos(cosine: float, dim: int = DIM) -> list[float]:
    """Vettore unitario con similarità ``cosine`` rispetto a ``basis(0)``."""
    vector = [0.0] * dim
    vector[0] = cosine
    vector[1] = (1.0 - cosine * cosine) ** 0.5
    return vector


class ControlledEmbed:
    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self.vectors: dict[str, list[float]] = {}

    def set(self, text_value: str, vector: list[float]) -> None:
        self.vectors[text_value] = vector

    def __call__(self, text_value: str) -> list[float]:
        return self.vectors.get(text_value) or _default_vec(text_value, self.dim)


# --------------------------------------------------------------------------- #
# Doppi: adapter sorgente e responder LLM
# --------------------------------------------------------------------------- #
class FakeAdapter:
    kind = "reddit"

    def __init__(self, *, pages=None, thread_pages=None, has_comments: bool = True) -> None:
        self.pages = list(pages or [])
        self.thread_pages = list(thread_pages or [])
        self.has_comments = has_comments
        self.fetch_new_calls = 0
        self.fetch_thread_calls = 0

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            has_comments=self.has_comments,
            supports_incremental=True,
            supports_backfill=False,
            supports_search=True,
        )

    def rate_policy(self) -> RatePolicy:
        return RatePolicy(
            requests_per_minute=100, burst=100, cost_per_call_usd=0.0, min_interval_ms=0
        )

    async def fetch_new(self, target, cursor, limit) -> FetchPage:
        self.fetch_new_calls += 1
        if not self.pages:
            return FetchPage(items=[], next_cursor=None, exhausted=True)
        return self.pages.pop(0)

    async def fetch_thread(self, thread_ref, since) -> FetchPage:
        self.fetch_thread_calls += 1
        if not self.thread_pages:
            return FetchPage(items=[], next_cursor=None, exhausted=True)
        return self.thread_pages.pop(0)

    def normalize(self, payload):  # pragma: no cover - non usato dai test
        raise NotImplementedError


def triage_payload(**over) -> dict:
    item = {
        "index": 0,
        "confidence": 0.9,
        "category": "product_idea",
        "one_line_summary": "sintesi",
        "problem": None,
        "audience": None,
        "tags": ["tag"],
        "dup_of_idea_id": None,
    }
    item.update(over)
    return {"results": [item]}


def analysis_payload(**over) -> dict:
    base = {
        "problem": "problema",
        "target_customer": "clienti",
        "current_alternatives": ["fogli di calcolo"],
        "proposed_solution": "soluzione",
        "mvp_scope": ["area"],
        "differentiators": ["velocità"],
        "monetization": {
            "model": "subscription",
            "price_hypothesis": "9 EUR/mese",
            "unit_economics_note": "nota",
        },
        "feasibility": {
            "score": 4,
            "rationale": "r",
            "hard_blockers": [],
            "tech_stack_hint": ["py"],
        },
        "economics": {"score": 4, "tam_signal": "grande", "rationale": "r"},
        "competition": {"score": 2, "named_players": ["x"], "rationale": "r"},
        "risks": [{"risk": "rischio", "severity": 2, "mitigation": "mitigazione"}],
        "effort": {"weeks_to_mvp": 8, "team_size": 2, "confidence": 0.7},
        "evidence_quotes": [],
        "verdict": "promising",
        "confidence": 0.7,
        "notes": "nota",
    }
    base.update(over)
    return base


def make_responder(*, triage=None, analysis=None, summary=None):
    def resolve(value, call):
        if callable(value):
            return value(call)
        return value

    def responder(call):
        properties = (call.schema or {}).get("properties", {})
        keys = set(properties)
        if keys == {"title", "canonical_summary"}:
            return json.dumps(
                resolve(summary, call)
                or {"title": "Titolo aggiornato", "canonical_summary": "Sommario aggiornato"}
            )
        if "results" in keys:
            return json.dumps(resolve(triage, call) or triage_payload())
        if "feasibility" in keys:
            return json.dumps(resolve(analysis, call) or analysis_payload())
        raise AssertionError(f"schema inatteso: {sorted(keys)}")

    return responder


# --------------------------------------------------------------------------- #
# Costruzione del contesto e helper DB
# --------------------------------------------------------------------------- #
def make_ctx(settings, session_factory, transport, adapter) -> HandlerContext:
    gateway = LlmGateway(settings, session_factory, {"local": transport}, NullLimiter())
    return HandlerContext(
        settings=settings,
        db=session_factory,
        queue=PgQueue(session_factory, settings),
        llm=gateway,
        rate=NullLimiter(),
        adapters={"reddit": adapter, "reddit_pullpush": adapter},
        now=lambda: datetime.now(UTC),
    )


async def run_handler(session_factory, ctx, topic: str, payload: dict) -> int:
    async with session_factory() as session:
        job_id = await session.scalar(
            text(
                "INSERT INTO jobs (topic, payload, state) "
                "VALUES (:topic, CAST(:payload AS jsonb), 'running') RETURNING id"
            ),
            {"topic": topic, "payload": json.dumps(payload)},
        )
        await session.commit()
    job = Job(
        id=job_id,
        topic=topic,
        payload=payload,
        state="running",
        attempts=1,
        max_attempts=5,
        locked_by="test",
        run_after=datetime.now(UTC),
    )
    await handler_for(topic)(job, ctx)
    return job_id


async def fetch_jobs(session_factory, topic: str | None = None):
    async with session_factory() as session:
        query = "SELECT id, topic, payload, priority, state FROM jobs"
        params: dict = {}
        if topic is not None:
            query += " WHERE topic = :topic"
            params["topic"] = topic
        query += " ORDER BY id"
        return list((await session.execute(text(query), params)).mappings())


async def seed_source(session, *, kind: str = "reddit", config: dict | None = None) -> int:
    return int(
        await session.scalar(
            text(
                "INSERT INTO sources (kind, name, config) "
                "VALUES (:kind, :name, CAST(:config AS jsonb)) RETURNING id"
            ),
            {"kind": kind, "name": kind, "config": json.dumps(config or {})},
        )
    )


async def seed_target(session, source_id: int, *, ref: str = "r/test") -> int:
    return int(
        await session.scalar(
            text(
                "INSERT INTO source_targets (source_id, target_ref, target_kind) "
                "VALUES (:source_id, :ref, 'subreddit') RETURNING id"
            ),
            {"source_id": source_id, "ref": ref},
        )
    )


async def seed_idea(
    session,
    *,
    title: str = "Idea",
    summary: str = "Sommario canonico",
    status: str = "new",
    score: int | None = None,
    vector: list[float] | None = None,
    model: str = "local/bge-m3:567m",
) -> int:
    idea_id = int(
        await session.scalar(
            text(
                "INSERT INTO idea_clusters (title, canonical_summary, status, opportunity_score) "
                "VALUES (:title, :summary, :status, :score) RETURNING id"
            ),
            {"title": title, "summary": summary, "status": status, "score": score},
        )
    )
    if vector is not None:
        await session.execute(
            text(
                "INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec) "
                "VALUES ('idea', :idea_id, :model, :dim, CAST(:vec AS vector))"
            ),
            {
                "idea_id": idea_id,
                "model": model,
                "dim": len(vector),
                "vec": vec_literal(vector),
            },
        )
    return idea_id


async def insert_item(
    session,
    *,
    source_id: int,
    external_id: str,
    kind: str = "post",
    title: str | None = None,
    body: str = "corpo",
    state: str = "new",
    idea_id: int | None = None,
    score: int | None = None,
    num_comments: int | None = None,
    thread_external_id: str | None = None,
    raw: dict | None = None,
) -> int:
    return int(
        await session.scalar(
            text(
                """
INSERT INTO items (source_id, external_id, kind, thread_external_id, title, body,
                   content_hash, state, created_at, score, num_comments, raw, idea_id)
VALUES (:source_id, :external_id, :kind, :thread, :title, :body,
        :content_hash, :state, now(), :score, :num_comments, CAST(:raw AS jsonb), :idea_id)
RETURNING id
"""
            ),
            {
                "source_id": source_id,
                "external_id": external_id,
                "kind": kind,
                "thread": thread_external_id or external_id,
                "title": title,
                "body": body,
                "content_hash": content_hash(body),
                "state": state,
                "score": score,
                "num_comments": num_comments,
                "raw": json.dumps(raw) if raw is not None else None,
                "idea_id": idea_id,
            },
        )
    )


async def link_item(session, idea_id: int, item_id: int, role: str = "seed") -> None:
    await session.execute(
        text("INSERT INTO idea_items (idea_id, item_id, role) VALUES (:idea_id, :item_id, :role)"),
        {"idea_id": idea_id, "item_id": item_id, "role": role},
    )


async def seed_analysis(
    session,
    *,
    idea_id: int,
    revision: int = 1,
    verdict: str = "promising",
    confidence: float = 0.7,
    feasibility: int = 4,
    economics: int = 4,
    competition: int = 2,
    payload: dict | None = None,
) -> int:
    return int(
        await session.scalar(
            text(
                """
INSERT INTO analyses (idea_id, revision, model_spec, provider, prompt_version,
                      schema_version, payload, feasibility_score, economics_score,
                      competition_score, verdict, confidence)
VALUES (:idea_id, :revision, 'local/qwen3:8b', 'local', 'v1', 'v1', CAST(:payload AS jsonb),
        :feasibility, :economics, :competition, :verdict, :confidence)
RETURNING id
"""
            ),
            {
                "idea_id": idea_id,
                "revision": revision,
                "payload": json.dumps(payload or analysis_payload()),
                "feasibility": feasibility,
                "economics": economics,
                "competition": competition,
                "verdict": verdict,
                "confidence": confidence,
            },
        )
    )


def norm_item(
    *,
    external_id: str,
    body: str,
    kind: str = "post",
    title: str | None = None,
    thread_external_id: str | None = None,
    parent_external_id: str | None = None,
    score: int | None = None,
    num_comments: int | None = None,
    created_at: datetime | None = None,
) -> NormalizedItem:
    return NormalizedItem(
        source_kind="reddit",
        external_id=external_id,
        kind=kind,
        thread_external_id=thread_external_id or external_id,
        parent_external_id=parent_external_id,
        title=title,
        body=body,
        author_hash=None,
        url=None,
        score=score,
        num_comments=num_comments,
        lang=None,
        created_at=created_at or datetime.now(UTC),
        edited_at=None,
        raw={"id": external_id},
        content_hash=content_hash(body),
    )


# --------------------------------------------------------------------------- #
# Cancelli G0 / G1 / G2
# --------------------------------------------------------------------------- #
async def test_g0_crosspost_rejected_exact_dup(session_factory, settings):
    adapter = FakeAdapter()
    transport = FakeTransport(responder=make_responder(), embed_fn=ControlledEmbed())
    ctx = make_ctx(settings, session_factory, transport, adapter)

    async with session_factory() as session:
        source_id = await seed_source(session)
        target_id = await seed_target(session, source_id)
        await session.commit()

    adapter.pages.append(
        FetchPage(
            items=[
                norm_item(external_id="t3_a", body="stesso corpo identico", title="A"),
                norm_item(external_id="t3_b", body="stesso corpo identico", title="B"),
            ],
            next_cursor={"last_fullname": "t3_b"},
            exhausted=True,
        )
    )
    await run_handler(session_factory, ctx, "pipeline.scrape", {"target_id": target_id})

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT external_id, state, reject_reason FROM items ORDER BY external_id")
                )
            )
            .mappings()
            .all()
        )
    by_ext = {row["external_id"]: row for row in rows}
    assert by_ext["t3_a"]["state"] == "new"
    assert by_ext["t3_b"]["state"] == "rejected"
    assert by_ext["t3_b"]["reject_reason"] == "exact_dup"
    assert transport.chat_calls == 0
    assert transport.embed_calls == 0

    triage_jobs = await fetch_jobs(session_factory, "pipeline.triage")
    assert len(triage_jobs) == 1
    assert len(triage_jobs[0]["payload"]["item_ids"]) == 1
    async with session_factory() as session:
        kept = await session.scalar(text("SELECT id FROM items WHERE external_id = 't3_a'"))
    assert triage_jobs[0]["payload"]["item_ids"] == [kept]


async def test_upsert_keeps_state_and_updates_metrics(session_factory, settings):
    adapter = FakeAdapter()
    transport = FakeTransport(responder=make_responder(), embed_fn=ControlledEmbed())
    ctx = make_ctx(settings, session_factory, transport, adapter)

    async with session_factory() as session:
        source_id = await seed_source(session)
        target_id = await seed_target(session, source_id)
        await session.commit()

    adapter.pages.append(
        FetchPage(
            items=[norm_item(external_id="t3_x", body="corpo", title="X", score=5)],
            next_cursor={"last_fullname": "t3_x"},
            exhausted=True,
        )
    )
    await run_handler(session_factory, ctx, "pipeline.scrape", {"target_id": target_id})

    async with session_factory() as session:
        await session.execute(text("UPDATE items SET state = 'triaged', category = 'pain_point'"))
        await session.commit()

    triage_before = len(await fetch_jobs(session_factory, "pipeline.triage"))

    adapter.pages.append(
        FetchPage(
            items=[
                norm_item(
                    external_id="t3_x",
                    body="corpo",
                    title="X",
                    score=50,
                    num_comments=7,
                )
            ],
            next_cursor={"last_fullname": "t3_x"},
            exhausted=True,
        )
    )
    await run_handler(session_factory, ctx, "pipeline.scrape", {"target_id": target_id})

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT state, score, num_comments FROM items WHERE external_id = 't3_x'")
                )
            )
            .mappings()
            .one()
        )
    assert row["state"] == "triaged"
    assert row["score"] == 50
    assert row["num_comments"] == 7
    assert len(await fetch_jobs(session_factory, "pipeline.triage")) == triage_before


async def test_g1_high_similarity_attaches_without_llm(session_factory, settings):
    embed = ControlledEmbed()
    transport = FakeTransport(responder=make_responder(), embed_fn=embed)
    adapter = FakeAdapter()
    ctx = make_ctx(settings, session_factory, transport, adapter)

    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id = await seed_idea(session, vector=basis(0))
        item_id = await insert_item(
            session, source_id=source_id, external_id="t3_g1", body="G1BODY"
        )
        await session.commit()

    embed.set("\nG1BODY", with_cos(0.97))

    await run_handler(session_factory, ctx, "pipeline.triage", {"item_ids": [item_id]})

    assert transport.chat_calls == 0
    async with session_factory() as session:
        item = (
            (
                await session.execute(
                    text("SELECT state, idea_id FROM items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )
        link = (
            (
                await session.execute(
                    text("SELECT role, similarity FROM idea_items WHERE item_id = :id"),
                    {"id": item_id},
                )
            )
            .mappings()
            .one()
        )
    assert item["state"] == "candidate"
    assert item["idea_id"] == idea_id
    assert link["role"] == "evidence"
    assert link["similarity"] > 0.92
    assert len(await fetch_jobs(session_factory, "pipeline.score")) == 1


async def test_g2_band_calls_model_and_attaches_on_dup_of_idea_id(session_factory, settings):
    embed = ControlledEmbed()
    state: dict = {}
    prompts: list[str] = []

    def triage_response(call):
        prompts.append(call.user or "")
        return triage_payload(dup_of_idea_id=state["target"])

    transport = FakeTransport(responder=make_responder(triage=triage_response), embed_fn=embed)
    adapter = FakeAdapter()
    ctx = make_ctx(settings, session_factory, transport, adapter)

    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_ids = []
        for index in range(5):
            idea_ids.append(
                await seed_idea(
                    session,
                    title=f"Idea {index}",
                    summary=f"sommario-{index}",
                    vector=basis(index) if index else basis(0),
                )
            )
        item_id = await insert_item(
            session, source_id=source_id, external_id="t3_g2", body="G2BODY"
        )
        await session.commit()

    embed.set("\nG2BODY", with_cos(0.85))
    state["target"] = idea_ids[0]
    target = idea_ids[0]

    await run_handler(session_factory, ctx, "pipeline.triage", {"item_ids": [item_id]})

    assert transport.chat_calls == 1
    assert all(f"sommario-{i}" in prompts[0] for i in range(5))

    cluster_jobs = await fetch_jobs(session_factory, "pipeline.cluster")
    assert len(cluster_jobs) == 1
    assert cluster_jobs[0]["payload"]["dup_of_idea_id"] == target

    await run_handler(session_factory, ctx, "pipeline.cluster", cluster_jobs[0]["payload"])

    async with session_factory() as session:
        item = (
            (
                await session.execute(
                    text("SELECT state, idea_id FROM items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )
        role = await session.scalar(
            text("SELECT role FROM idea_items WHERE idea_id = :idea AND item_id = :item"),
            {"idea": target, "item": item_id},
        )
    assert item["state"] == "candidate"
    assert item["idea_id"] == target
    assert role == "evidence"


async def test_market_signal_without_neighbour_rejected_not_idea(session_factory, settings):
    transport = FakeTransport(
        responder=make_responder(triage=triage_payload(category="market_signal")),
        embed_fn=ControlledEmbed(),
    )
    adapter = FakeAdapter()
    ctx = make_ctx(settings, session_factory, transport, adapter)

    async with session_factory() as session:
        source_id = await seed_source(session)
        item_id = await insert_item(
            session, source_id=source_id, external_id="t3_ms", body="MSBODY"
        )
        await session.commit()

    await run_handler(session_factory, ctx, "pipeline.triage", {"item_ids": [item_id]})

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT state, reject_reason FROM items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )
    assert row["state"] == "rejected"
    assert row["reject_reason"] == "not_idea"
    assert await fetch_jobs(session_factory, "pipeline.cluster") == []


# --------------------------------------------------------------------------- #
# Cluster: nascita di un'idea
# --------------------------------------------------------------------------- #
async def _triage_then_cluster(session_factory, settings, *, has_comments: bool):
    transport = FakeTransport(
        responder=make_responder(triage=triage_payload(category="product_idea")),
        embed_fn=ControlledEmbed(),
    )
    adapter = FakeAdapter(has_comments=has_comments)
    ctx = make_ctx(settings, session_factory, transport, adapter)

    async with session_factory() as session:
        source_id = await seed_source(session)
        item_id = await insert_item(
            session,
            source_id=source_id,
            external_id="t3_new",
            title="Nuova idea",
            body="Descrizione del problema da risolvere.",
        )
        await session.commit()

    await run_handler(session_factory, ctx, "pipeline.triage", {"item_ids": [item_id]})
    cluster_jobs = await fetch_jobs(session_factory, "pipeline.cluster")
    assert len(cluster_jobs) == 1
    await run_handler(session_factory, ctx, "pipeline.cluster", cluster_jobs[0]["payload"])

    async with session_factory() as session:
        item = (
            (
                await session.execute(
                    text("SELECT state, idea_id FROM items WHERE id = :id"), {"id": item_id}
                )
            )
            .mappings()
            .one()
        )
        seed_role = await session.scalar(
            text("SELECT role FROM idea_items WHERE item_id = :id"), {"id": item_id}
        )
        idea_exists = await session.scalar(
            text("SELECT count(*) FROM idea_clusters WHERE id = :id"), {"id": item["idea_id"]}
        )
        embedding = await session.scalar(
            text("SELECT count(*) FROM embeddings WHERE owner_kind = 'idea' AND owner_id = :id"),
            {"id": item["idea_id"]},
        )
    return item, seed_role, idea_exists, embedding


async def test_new_idea_seed_enqueues_initial_watch_for_has_comments_source(
    session_factory, settings
):
    item, seed_role, idea_exists, embedding = await _triage_then_cluster(
        session_factory, settings, has_comments=True
    )
    assert item["state"] == "candidate"
    assert item["idea_id"] is not None
    assert seed_role == "seed"
    assert idea_exists == 1
    assert embedding == 1

    watch_jobs = await fetch_jobs(session_factory, "pipeline.watch")
    assert len(watch_jobs) == 1
    assert watch_jobs[0]["priority"] == 50
    assert watch_jobs[0]["payload"] == {"idea_id": item["idea_id"], "initial": True}
    assert await fetch_jobs(session_factory, "pipeline.analyze") == []

    # Il sommario canonico è quello del triage, non un troncamento del corpo: è il
    # testo su cui G1/G2 confrontano l'idea (§8.2).
    async with session_factory() as session:
        summary = await session.scalar(
            text("SELECT canonical_summary FROM idea_clusters WHERE id = :id"),
            {"id": item["idea_id"]},
        )
    assert summary == "sintesi"


async def test_enqueues_analyze_when_source_lacks_comments(session_factory, settings):
    item, _, _, _ = await _triage_then_cluster(session_factory, settings, has_comments=False)
    analyze_jobs = await fetch_jobs(session_factory, "pipeline.analyze")
    assert len(analyze_jobs) == 1
    assert analyze_jobs[0]["payload"] == {"idea_id": item["idea_id"], "reason": "initial"}
    assert await fetch_jobs(session_factory, "pipeline.watch") == []


# --------------------------------------------------------------------------- #
# Analyze
# --------------------------------------------------------------------------- #
async def _seed_analysis_idea(session_factory, settings, *, verdict="promising", **payload_over):
    transport = FakeTransport(
        responder=make_responder(analysis=analysis_payload(verdict=verdict, **payload_over)),
        embed_fn=ControlledEmbed(),
    )
    adapter = FakeAdapter()
    ctx = make_ctx(settings, session_factory, transport, adapter)
    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id = await seed_idea(session, status="analyzed")
        seed_item = await insert_item(
            session,
            source_id=source_id,
            external_id="t3_seed",
            title="Problema",
            body="Descrivo il problema",
            state="analyzed",
            score=100,
            num_comments=10,
            raw={"original": True},
        )
        await link_item(session, idea_id, seed_item, "seed")
        await seed_analysis(session, idea_id=idea_id, revision=1)
        await session.commit()
    return ctx, transport, idea_id, seed_item


async def test_analyze_creates_revision_two_and_supersedes_first(session_factory, settings):
    ctx, transport, idea_id, seed_item = await _seed_analysis_idea(session_factory, settings)

    await run_handler(
        session_factory, ctx, "pipeline.analyze", {"idea_id": idea_id, "reason": "manual"}
    )

    async with session_factory() as session:
        revisions = (
            (
                await session.execute(
                    text(
                        "SELECT id, revision, superseded_by, opportunity_score FROM analyses "
                        "WHERE idea_id = :id ORDER BY revision"
                    ),
                    {"id": idea_id},
                )
            )
            .mappings()
            .all()
        )
        idea = (
            (
                await session.execute(
                    text("SELECT title, canonical_summary FROM idea_clusters WHERE id = :id"),
                    {"id": idea_id},
                )
            )
            .mappings()
            .one()
        )
        updates = (
            (
                await session.execute(
                    text("SELECT kind FROM idea_updates WHERE idea_id = :id"), {"id": idea_id}
                )
            )
            .scalars()
            .all()
        )
    assert [row["revision"] for row in revisions] == [1, 2]
    assert revisions[0]["superseded_by"] == revisions[1]["id"]
    assert revisions[1]["superseded_by"] is None
    assert revisions[1]["opportunity_score"] is not None
    assert idea["title"] == "Titolo aggiornato"
    assert idea["canonical_summary"] == "Sommario aggiornato"
    assert "analysis_revision" in updates
    assert len(await fetch_jobs(session_factory, "pipeline.score")) == 1


async def test_analyze_zeroes_raw(session_factory, settings):
    ctx, transport, idea_id, seed_item = await _seed_analysis_idea(session_factory, settings)
    await run_handler(
        session_factory, ctx, "pipeline.analyze", {"idea_id": idea_id, "reason": "manual"}
    )
    async with session_factory() as session:
        raw = await session.scalar(text("SELECT raw FROM items WHERE id = :id"), {"id": seed_item})
    assert raw is None


async def test_analyze_drops_invalid_quote_ids(session_factory, settings):
    settings.quote_max_chars = 10
    payload = analysis_payload(
        evidence_quotes=[
            {"item_external_id": "t3_seed", "quote": "x" * 50},
            {"item_external_id": "t1_bogus", "quote": "y" * 50},
        ]
    )
    transport = FakeTransport(
        responder=make_responder(analysis=payload), embed_fn=ControlledEmbed()
    )
    adapter = FakeAdapter()
    ctx = make_ctx(settings, session_factory, transport, adapter)
    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id = await seed_idea(session, status="analyzed")
        seed_item = await insert_item(
            session, source_id=source_id, external_id="t3_seed", body="corpo", state="analyzed"
        )
        await link_item(session, idea_id, seed_item, "seed")
        await session.commit()

    await run_handler(
        session_factory, ctx, "pipeline.analyze", {"idea_id": idea_id, "reason": "manual"}
    )

    async with session_factory() as session:
        stored = await session.scalar(
            text("SELECT payload FROM analyses WHERE idea_id = :id AND revision = 1"),
            {"id": idea_id},
        )
    quotes = stored["evidence_quotes"]
    assert len(quotes) == 1
    assert quotes[0]["item_external_id"] == "t3_seed"
    assert quotes[0]["quote"] == "x" * 10


async def test_verdict_reject_disables_watch_and_sets_idea_rejected(session_factory, settings):
    ctx, transport, idea_id, seed_item = await _seed_analysis_idea(
        session_factory, settings, verdict="reject"
    )
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO watchlist (idea_id, enabled, interval_s, mode, added_by) "
                "VALUES (:id, true, 3600, 'thread_full', 'manual')"
            ),
            {"id": idea_id},
        )
        await session.commit()

    await run_handler(
        session_factory, ctx, "pipeline.analyze", {"idea_id": idea_id, "reason": "manual"}
    )

    async with session_factory() as session:
        status = await session.scalar(
            text("SELECT status FROM idea_clusters WHERE id = :id"), {"id": idea_id}
        )
        enabled = await session.scalar(
            text("SELECT enabled FROM watchlist WHERE idea_id = :id"), {"id": idea_id}
        )
    assert status == "rejected"
    assert enabled is False


# --------------------------------------------------------------------------- #
# Score e watch automatico
# --------------------------------------------------------------------------- #
async def test_score_creates_auto_watch_above_threshold_and_never_reenables(
    session_factory, settings
):
    transport = FakeTransport(responder=make_responder(), embed_fn=ControlledEmbed())
    adapter = FakeAdapter(has_comments=True)
    ctx = make_ctx(settings, session_factory, transport, adapter)

    high = analysis_payload(
        feasibility={"score": 5, "rationale": "r", "hard_blockers": [], "tech_stack_hint": ["py"]},
        economics={"score": 5, "tam_signal": "t", "rationale": "r"},
        competition={"score": 1, "named_players": ["x"], "rationale": "r"},
        evidence_quotes=[{"item_external_id": "t3_seed", "quote": f"q{i}"} for i in range(5)],
        verdict="strong",
        confidence=1.0,
    )
    async with session_factory() as session:
        source_id = await seed_source(
            session, config={"engagement_saturation": 5000, "cost_per_call_usd": 0.0}
        )
        idea_id = await seed_idea(session, status="analyzed")
        seed_item = await insert_item(
            session,
            source_id=source_id,
            external_id="t3_seed",
            body="corpo",
            state="analyzed",
            score=3000,
            num_comments=500,
        )
        await link_item(session, idea_id, seed_item, "seed")
        await seed_analysis(session, idea_id=idea_id, revision=1, payload=high)
        await session.commit()

    await run_handler(session_factory, ctx, "pipeline.score", {"idea_id": idea_id})

    async with session_factory() as session:
        idea = (
            (
                await session.execute(
                    text("SELECT status, opportunity_score FROM idea_clusters WHERE id = :id"),
                    {"id": idea_id},
                )
            )
            .mappings()
            .one()
        )
        watch = (
            (
                await session.execute(
                    text("SELECT enabled, mode, added_by FROM watchlist WHERE idea_id = :id"),
                    {"id": idea_id},
                )
            )
            .mappings()
            .one()
        )
    assert idea["opportunity_score"] >= settings.watch_threshold
    assert idea["status"] == "watching"
    assert watch["enabled"] is True
    assert watch["mode"] == "thread_full"
    assert watch["added_by"] == "auto"

    async with session_factory() as session:
        await session.execute(
            text("UPDATE watchlist SET enabled = false WHERE idea_id = :id"), {"id": idea_id}
        )
        await session.commit()
    await run_handler(session_factory, ctx, "pipeline.score", {"idea_id": idea_id})
    async with session_factory() as session:
        enabled = await session.scalar(
            text("SELECT enabled FROM watchlist WHERE idea_id = :id"), {"id": idea_id}
        )
    assert enabled is False


# --------------------------------------------------------------------------- #
# Watch
# --------------------------------------------------------------------------- #
async def _seed_watch(
    session_factory, *, mode="thread_full", interval_s=3600, last_change=None, last_checked=None
):
    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id = await seed_idea(session, status="watching")
        seed_item = await insert_item(
            session,
            source_id=source_id,
            external_id="t3_w",
            title="Thread",
            body="corpo del post",
            state="analyzed",
            score=10,
            num_comments=2,
            thread_external_id="t3_w",
        )
        await link_item(session, idea_id, seed_item, "seed")
        await session.execute(
            text(
                "INSERT INTO watchlist (idea_id, enabled, interval_s, mode, added_by, "
                "last_change_at, last_checked_at) "
                "VALUES (:idea_id, true, :interval, :mode, 'manual', :change, :checked)"
            ),
            {
                "idea_id": idea_id,
                "interval": interval_s,
                "mode": mode,
                "change": last_change,
                "checked": last_checked,
            },
        )
        await session.commit()
    return idea_id, source_id


async def _watch_ctx(session_factory, settings, adapter):
    transport = FakeTransport(
        responder=make_responder(analysis=analysis_payload()), embed_fn=ControlledEmbed()
    )
    return make_ctx(settings, session_factory, transport, adapter)


async def test_watch_delta_creates_revision_without_new_idea(session_factory, settings):
    idea_id, source_id = await _seed_watch(
        session_factory, last_checked=datetime.now(UTC) - timedelta(hours=1)
    )
    adapter = FakeAdapter(
        thread_pages=[
            FetchPage(
                items=[
                    norm_item(external_id="t3_w", body="corpo del post", title="Thread", score=20),
                    norm_item(
                        external_id="t1_c1",
                        kind="comment",
                        body="commento uno",
                        thread_external_id="t3_w",
                        parent_external_id="t3_w",
                        score=3,
                    ),
                    norm_item(
                        external_id="t1_c2",
                        kind="comment",
                        body="commento due",
                        thread_external_id="t3_w",
                        parent_external_id="t3_w",
                        score=2,
                    ),
                    norm_item(
                        external_id="t1_c3",
                        kind="comment",
                        body="commento tre",
                        thread_external_id="t3_w",
                        parent_external_id="t3_w",
                        score=1,
                    ),
                ],
                next_cursor=None,
                exhausted=True,
            )
        ]
    )
    ctx = await _watch_ctx(session_factory, settings, adapter)

    await run_handler(session_factory, ctx, "pipeline.watch", {"idea_id": idea_id})

    analyze_jobs = await fetch_jobs(session_factory, "pipeline.analyze")
    assert len(analyze_jobs) == 1
    assert analyze_jobs[0]["payload"]["reason"] == "watch_delta"

    # L'analisi non crea una seconda idea: si aggiunge solo una revisione.
    await run_handler(session_factory, ctx, "pipeline.analyze", analyze_jobs[0]["payload"])
    async with session_factory() as session:
        ideas = await session.scalar(text("SELECT count(*) FROM idea_clusters"))
        revisions = await session.scalar(
            text("SELECT count(*) FROM analyses WHERE idea_id = :id"), {"id": idea_id}
        )
    assert ideas == 1
    assert revisions == 1


async def test_initial_watch_without_watchlist_row_enqueues_analysis(session_factory, settings):
    """§8.5: il watch iniziale accoda l'analisi anche senza riga in `watchlist`.

    La riga la crea `pipeline.score`, e solo sopra soglia: se il watch iniziale uscisse
    quando la riga manca, le idee nate da un post resterebbero senza analisi.
    """
    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id = await seed_idea(session, status="new")
        seed_item = await insert_item(
            session,
            source_id=source_id,
            external_id="t3_init",
            title="Thread",
            body="corpo del post",
            state="candidate",
            score=10,
            num_comments=0,
            thread_external_id="t3_init",
        )
        await link_item(session, idea_id, seed_item, "seed")
        await session.commit()

    adapter = FakeAdapter(
        thread_pages=[
            FetchPage(
                items=[
                    norm_item(
                        external_id="t3_init", body="corpo del post", title="Thread", score=12
                    ),
                    norm_item(
                        external_id="t1_init",
                        kind="comment",
                        body="un commento del thread",
                        thread_external_id="t3_init",
                        parent_external_id="t3_init",
                        score=4,
                    ),
                ],
                next_cursor=None,
                exhausted=True,
            )
        ]
    )
    ctx = await _watch_ctx(session_factory, settings, adapter)
    await run_handler(session_factory, ctx, "pipeline.watch", {"idea_id": idea_id, "initial": True})

    analyze_jobs = await fetch_jobs(session_factory, "pipeline.analyze")
    assert len(analyze_jobs) == 1
    assert analyze_jobs[0]["payload"] == {"idea_id": idea_id, "reason": "initial"}

    async with session_factory() as session:
        watch_rows = await session.scalar(text("SELECT count(*) FROM watchlist"))
        role = await session.scalar(
            text(
                "SELECT ii.role FROM idea_items ii JOIN items i ON i.id = ii.item_id "
                "WHERE ii.idea_id = :id AND i.external_id = 't1_init'"
            ),
            {"id": idea_id},
        )
    assert watch_rows == 0
    assert role == "update"


async def test_watch_adaptive_interval_doubles_and_resets(session_factory, settings):
    idea_id, _ = await _seed_watch(
        session_factory,
        mode="comments_only",
        interval_s=3600,
        last_checked=datetime.now(UTC) - timedelta(hours=1),
    )
    head_only = FetchPage(
        items=[norm_item(external_id="t3_w", body="corpo del post", title="Thread", score=10)],
        next_cursor=None,
        exhausted=True,
    )
    new_comment = FetchPage(
        items=[
            norm_item(external_id="t3_w", body="corpo del post", title="Thread", score=11),
            norm_item(
                external_id="t1_n1",
                kind="comment",
                body="nuovo",
                thread_external_id="t3_w",
                parent_external_id="t3_w",
            ),
        ],
        next_cursor=None,
        exhausted=True,
    )
    adapter = FakeAdapter(thread_pages=[head_only, new_comment])
    ctx = await _watch_ctx(session_factory, settings, adapter)

    await run_handler(session_factory, ctx, "pipeline.watch", {"idea_id": idea_id})
    async with session_factory() as session:
        interval = await session.scalar(
            text("SELECT interval_s FROM watchlist WHERE idea_id = :id"), {"id": idea_id}
        )
    assert interval == 7200

    await run_handler(session_factory, ctx, "pipeline.watch", {"idea_id": idea_id})
    async with session_factory() as session:
        interval = await session.scalar(
            text("SELECT interval_s FROM watchlist WHERE idea_id = :id"), {"id": idea_id}
        )
    assert interval == settings.watch_min_interval_s


async def test_watch_expire_pauses_and_notifies(session_factory, settings):
    old_change = datetime.now(UTC) - timedelta(days=settings.watch_expire_days + 1)
    idea_id, _ = await _seed_watch(
        session_factory,
        mode="comments_only",
        interval_s=3600,
        last_change=old_change,
        last_checked=datetime.now(UTC) - timedelta(hours=1),
    )
    adapter = FakeAdapter(
        thread_pages=[
            FetchPage(
                items=[
                    norm_item(external_id="t3_w", body="corpo del post", title="Thread", score=10)
                ],
                next_cursor=None,
                exhausted=True,
            )
        ]
    )
    ctx = await _watch_ctx(session_factory, settings, adapter)

    await run_handler(session_factory, ctx, "pipeline.watch", {"idea_id": idea_id})

    async with session_factory() as session:
        enabled = await session.scalar(
            text("SELECT enabled FROM watchlist WHERE idea_id = :id"), {"id": idea_id}
        )
        status = await session.scalar(
            text("SELECT status FROM idea_clusters WHERE id = :id"), {"id": idea_id}
        )
        updates = (
            (
                await session.execute(
                    text("SELECT kind FROM idea_updates WHERE idea_id = :id"), {"id": idea_id}
                )
            )
            .scalars()
            .all()
        )
    assert enabled is False
    assert status == "analyzed"
    assert "watch_paused" in updates


# --------------------------------------------------------------------------- #
# Retention e reembed
# --------------------------------------------------------------------------- #
async def test_retention_deletes_rejected_zeroes_raw_and_prunes_jobs_and_cache(
    session_factory, settings
):
    transport = FakeTransport(responder=make_responder(), embed_fn=ControlledEmbed())
    adapter = FakeAdapter()
    ctx = make_ctx(settings, session_factory, transport, adapter)

    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id = await seed_idea(session, status="analyzed")
        await seed_analysis(session, idea_id=idea_id, revision=1)

        old_rejected = await insert_item(
            session, source_id=source_id, external_id="t3_old", body="vecchio", state="rejected"
        )
        recent_rejected_item = await insert_item(
            session, source_id=source_id, external_id="t3_rec", body="recente", state="rejected"
        )
        assert recent_rejected_item
        analyzed = await insert_item(
            session,
            source_id=source_id,
            external_id="t3_an",
            body="analizzato",
            state="analyzed",
            idea_id=idea_id,
            raw={"x": 1},
        )
        await session.execute(
            text("UPDATE items SET fetched_at = now() - interval '40 days' WHERE id = :id"),
            {"id": old_rejected},
        )
        await session.execute(
            text(
                "INSERT INTO llm_cache (cache_key, model_spec, response, created_at) "
                "VALUES ('old-key', 'local/x', '{}'::jsonb, now() - interval '40 days'), "
                "('new-key', 'local/x', '{}'::jsonb, now())"
            )
        )
        await session.execute(
            text(
                "INSERT INTO jobs (topic, state, finished_at) VALUES "
                "('maintenance.retention', 'done', now() - interval '10 days'), "
                "('maintenance.retention', 'done', now())"
            )
        )
        await session.commit()

    await run_handler(session_factory, ctx, "maintenance.retention", {})

    async with session_factory() as session:
        items = (
            (await session.execute(text("SELECT external_id FROM items ORDER BY external_id")))
            .scalars()
            .all()
        )
        raw = await session.scalar(text("SELECT raw FROM items WHERE id = :id"), {"id": analyzed})
        cache = (await session.execute(text("SELECT cache_key FROM llm_cache"))).scalars().all()
        old_jobs = await session.scalar(
            text(
                "SELECT count(*) FROM jobs WHERE topic = 'maintenance.retention' "
                "AND state = 'done' AND finished_at < now() - interval '7 days'"
            )
        )
    assert "t3_old" not in items
    assert "t3_rec" in items
    assert raw is None
    assert cache == ["new-key"]
    assert old_jobs == 0


async def test_reembed_recomputes_and_reindexes(session_factory, settings):
    transport = FakeTransport(responder=make_responder(), embed_fn=ControlledEmbed())
    adapter = FakeAdapter()
    ctx = make_ctx(settings, session_factory, transport, adapter)

    async with session_factory() as session:
        source_id = await seed_source(session)
        idea_id = await seed_idea(session, title="Titolo", summary="Sommario")
        item_id = await insert_item(
            session, source_id=source_id, external_id="t3_re", body="corpo", state="triaged"
        )
        await session.execute(
            text(
                "INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec) "
                "VALUES ('idea', :idea_id, :model, 1024, CAST(:vec AS vector))"
            ),
            {
                "idea_id": idea_id,
                "model": settings.embed_model,
                "vec": vec_literal(basis(0)),
            },
        )
        await session.commit()
    stale = vec_literal(basis(0))

    await run_handler(session_factory, ctx, "maintenance.reembed", {"scope": "all"})

    async with session_factory() as session:
        idea_vec = await session.scalar(
            text(
                "SELECT CAST(vec AS text) FROM embeddings "
                "WHERE owner_kind = 'idea' AND owner_id = :id"
            ),
            {"id": idea_id},
        )
        item_rows = await session.scalar(
            text("SELECT count(*) FROM embeddings WHERE owner_kind = 'item' AND owner_id = :id"),
            {"id": item_id},
        )
        job_state = await session.scalar(
            text(
                "SELECT state FROM jobs WHERE topic = 'maintenance.reembed' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
    assert idea_vec != stale
    assert item_rows == 1
    assert job_state == "done"
