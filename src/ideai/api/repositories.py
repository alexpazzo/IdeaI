"""Query dell'API: viste, liste, merge/cancellazione transazionali (§9.1)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

IDEA_COLUMNS = """
    r.id, r.title, r.canonical_summary, r.status, r.opportunity_score, r.score_version,
    r.item_count, r.source_kinds, r.first_seen_at, r.last_activity_at, r.category, r.tags,
    r.watch_enabled, r.watch_mode, r.watch_interval_s, r.revision, r.verdict, r.confidence,
    r.feasibility_score, r.economics_score, r.competition_score, r.model_spec, r.analyzed_at
"""

IDEA_SOURCE = "FROM v_idea_ranking r"

_SORTS = {
    "opportunity_score": "opportunity_score DESC NULLS LAST, id DESC",
    "last_activity_at": "last_activity_at DESC, id DESC",
    "first_seen_at": "first_seen_at DESC, id DESC",
}

ANALYSIS_COLUMNS = """
    id, revision, superseded_by, verdict, confidence, feasibility_score, economics_score,
    competition_score, opportunity_score, model_spec, provider, prompt_version, schema_version,
    tokens_in, tokens_out, cost_usd, latency_ms, created_at, payload
"""


def idea_row(row) -> dict[str, Any]:
    """Riga di ``v_idea_ranking`` → forma di ``IdeaListItem``."""
    data = dict(row._mapping)
    return {
        "id": data["id"],
        "title": data["title"],
        "canonical_summary": data["canonical_summary"],
        "status": data["status"],
        "opportunity_score": data["opportunity_score"],
        "score_version": data["score_version"],
        "verdict": data["verdict"],
        "confidence": data["confidence"],
        "category": data["category"],
        "tags": data["tags"] or [],
        "source_kinds": data["source_kinds"] or [],
        "item_count": data["item_count"],
        "first_seen_at": data["first_seen_at"],
        "last_activity_at": data["last_activity_at"],
        "watch": {
            "enabled": bool(data["watch_enabled"]),
            "mode": data["watch_mode"] or "thread_full",
            "interval_s": data["watch_interval_s"]
            if data["watch_interval_s"] is not None
            else 3600,
        },
    }


def analysis_row(row) -> dict[str, Any]:
    data = dict(row._mapping)
    data.pop("id", None)
    return data


def _idea_filters(
    *,
    min_score: int | None,
    max_score: int | None,
    verdict: str | None,
    status: str | None,
    category: str | None,
    tag: str | None,
    source_kind: str | None,
    date_from,
    date_to,
) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if min_score is not None:
        where.append("opportunity_score >= :min_score")
        params["min_score"] = min_score
    if max_score is not None:
        where.append("opportunity_score <= :max_score")
        params["max_score"] = max_score
    if verdict is not None:
        where.append("verdict = :verdict")
        params["verdict"] = verdict
    if status is not None:
        where.append("status = :status")
        params["status"] = status
    if category is not None:
        where.append("category = :category")
        params["category"] = category
    if tag is not None:
        where.append("CAST(:tag AS text) = ANY(tags)")
        params["tag"] = tag
    if source_kind is not None:
        where.append("CAST(:source_kind AS text) = ANY(source_kinds)")
        params["source_kind"] = source_kind
    if date_from is not None:
        where.append("first_seen_at >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        where.append("first_seen_at <= :date_to")
        params["date_to"] = date_to
    return where, params


async def list_ideas(
    session: AsyncSession,
    *,
    min_score: int | None = None,
    max_score: int | None = None,
    verdict: str | None = None,
    status: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    source_kind: str | None = None,
    date_from=None,
    date_to=None,
    sort: str = "opportunity_score",
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    where, params = _idea_filters(
        min_score=min_score,
        max_score=max_score,
        verdict=verdict,
        status=status,
        category=category,
        tag=tag,
        source_kind=source_kind,
        date_from=date_from,
        date_to=date_to,
    )
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = await session.scalar(text(f"SELECT count(*) FROM v_idea_ranking r {clause}"), params)
    rows = (
        await session.execute(
            text(
                f"SELECT {IDEA_COLUMNS} {IDEA_SOURCE} {clause} "
                f"ORDER BY {_SORTS[sort]} LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": limit, "offset": offset},
        )
    ).all()
    return int(total or 0), [idea_row(r) for r in rows]


async def search_ideas(
    session: AsyncSession,
    *,
    q: str,
    vec: str,
    model: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fusione RRF fra ranking full-text e ranking vettoriale (§9.1)."""
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    rows = (
        await session.execute(
            text(
                f"""
WITH fts AS (
    SELECT r.id,
           row_number() OVER (
               ORDER BY ts_rank(to_tsvector('simple', r.title || ' ' || r.canonical_summary),
                                websearch_to_tsquery('simple', :q)) DESC, r.id
           ) AS rank
      FROM v_idea_ranking r
     WHERE to_tsvector('simple', r.title || ' ' || r.canonical_summary)
           @@ websearch_to_tsquery('simple', :q)
     LIMIT 200
), vec AS (
    SELECT owner_id AS id,
           row_number() OVER (ORDER BY vec <=> CAST(:vec AS vector), owner_id) AS rank
      FROM embeddings
     WHERE owner_kind = 'idea' AND model = :model
     ORDER BY vec <=> CAST(:vec AS vector)
     LIMIT 200
), merged AS (
    SELECT COALESCE(f.id, v.id) AS id,
           1.0 / (60 + COALESCE(f.rank, 1000000))
         + 1.0 / (60 + COALESCE(v.rank, 1000000)) AS score
      FROM fts f FULL OUTER JOIN vec v ON f.id = v.id
)
SELECT {IDEA_COLUMNS}
  FROM merged m JOIN v_idea_ranking r ON r.id = m.id
 ORDER BY m.score DESC, r.id
 LIMIT :limit
"""
            ),
            {"q": q, "vec": vec, "model": model, "limit": limit},
        )
    ).all()
    return [idea_row(r) for r in rows]


async def get_idea(session: AsyncSession, idea_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(f"SELECT {IDEA_COLUMNS} {IDEA_SOURCE} WHERE r.id = :id"),
            {"id": idea_id},
        )
    ).first()
    return idea_row(row) if row else None


async def current_analysis(session: AsyncSession, idea_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                f"SELECT {ANALYSIS_COLUMNS} FROM analyses "
                "WHERE idea_id = :id AND superseded_by IS NULL "
                "ORDER BY revision DESC LIMIT 1"
            ),
            {"id": idea_id},
        )
    ).first()
    return analysis_row(row) if row else None


async def list_revisions(session: AsyncSession, idea_id: int) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                f"SELECT {ANALYSIS_COLUMNS} FROM analyses WHERE idea_id = :id "
                "ORDER BY revision DESC"
            ),
            {"id": idea_id},
        )
    ).all()
    return [analysis_row(r) for r in rows]


async def list_idea_items(session: AsyncSession, idea_id: int) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
SELECT i.id, i.external_id, i.kind, i.title, i.body, i.url, i.score, i.num_comments,
       ii.role, ii.similarity, i.state
  FROM idea_items ii JOIN items i ON i.id = ii.item_id
 WHERE ii.idea_id = :id
 ORDER BY ii.added_at, i.id
"""
            ),
            {"id": idea_id},
        )
    ).all()
    return [
        {
            "id": r._mapping["id"],
            "external_id": r._mapping["external_id"],
            "kind": r._mapping["kind"],
            "title": r._mapping["title"],
            "body": r._mapping["body"],
            "url": r._mapping["url"],
            "score": r._mapping["score"],
            "num_comments": r._mapping["num_comments"],
            "role": r._mapping["role"],
            "similarity": r._mapping["similarity"],
            "state": r._mapping["state"],
        }
        for r in rows
    ]


async def list_timeline(session: AsyncSession, idea_id: int) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "SELECT id, item_id, kind, summary, created_at FROM idea_updates "
                "WHERE idea_id = :id ORDER BY created_at DESC, id DESC LIMIT 200"
            ),
            {"id": idea_id},
        )
    ).all()
    return [dict(r._mapping) for r in rows]


async def idea_exists(session: AsyncSession, idea_id: int) -> bool:
    return bool(
        await session.scalar(text("SELECT 1 FROM idea_clusters WHERE id = :id"), {"id": idea_id})
    )


async def upsert_watch(
    session: AsyncSession, idea_id: int, *, enabled: bool, mode: str, interval_s: int
) -> None:
    await session.execute(
        text(
            """
INSERT INTO watchlist (idea_id, enabled, interval_s, mode, added_by,
                       last_checked_at, last_change_at)
VALUES (:id, :enabled, :interval_s, :mode, 'manual', now(), now())
ON CONFLICT (idea_id) DO UPDATE
   SET enabled = EXCLUDED.enabled,
       interval_s = EXCLUDED.interval_s,
       mode = EXCLUDED.mode
"""
        ),
        {"id": idea_id, "enabled": enabled, "interval_s": interval_s, "mode": mode},
    )
    await session.execute(
        text("UPDATE idea_clusters SET status = :status WHERE id = :id"),
        {"id": idea_id, "status": "watching" if enabled else "analyzed"},
    )


async def merge_ideas(session: AsyncSession, source_id: int, into_id: int) -> None:
    """Fusione transazionale: item, timeline, watch e stato dell'idea sorgente (§9.1)."""
    await session.execute(
        text(
            """
INSERT INTO idea_items (idea_id, item_id, role, similarity, added_at)
SELECT :into_id, item_id, role, similarity, added_at
  FROM idea_items WHERE idea_id = :source_id
ON CONFLICT (idea_id, item_id) DO NOTHING
"""
        ),
        {"into_id": into_id, "source_id": source_id},
    )
    await session.execute(text("DELETE FROM idea_items WHERE idea_id = :id"), {"id": source_id})
    await session.execute(
        text("UPDATE items SET idea_id = :into_id WHERE idea_id = :source_id"),
        {"into_id": into_id, "source_id": source_id},
    )
    await session.execute(
        text("UPDATE idea_updates SET idea_id = :into_id WHERE idea_id = :source_id"),
        {"into_id": into_id, "source_id": source_id},
    )
    await session.execute(
        text("UPDATE idea_clusters SET status = 'archived' WHERE id = :id"), {"id": source_id}
    )
    await session.execute(
        text("UPDATE watchlist SET enabled = false WHERE idea_id = :id"), {"id": source_id}
    )
    await session.execute(
        text(
            "INSERT INTO idea_updates (idea_id, item_id, kind, summary) "
            "VALUES (:into_id, NULL, 'merged', :summary)"
        ),
        {"into_id": into_id, "summary": f"Idea {source_id} fusa in questa idea"},
    )


async def delete_idea(session: AsyncSession, idea_id: int) -> bool:
    """Cancella idea, item collegati solo a lei ed embedding orfani (§9.1)."""
    await session.execute(
        text(
            "DELETE FROM embeddings WHERE owner_kind = 'item' AND owner_id IN "
            "(SELECT item_id FROM idea_items WHERE idea_id = :id)"
        ),
        {"id": idea_id},
    )
    await session.execute(
        text("DELETE FROM embeddings WHERE owner_kind = 'idea' AND owner_id = :id"),
        {"id": idea_id},
    )
    await session.execute(
        text(
            "DELETE FROM items WHERE idea_id = :id AND NOT EXISTS "
            "(SELECT 1 FROM idea_items WHERE item_id = items.id AND idea_id <> :id)"
        ),
        {"id": idea_id},
    )
    result = await session.execute(
        text("DELETE FROM idea_clusters WHERE id = :id"), {"id": idea_id}
    )
    return (result.rowcount or 0) > 0


async def list_sources(session: AsyncSession, rate_limits_for) -> list[dict[str, Any]]:
    sources = (
        await session.execute(
            text("SELECT id, kind, name, enabled, config FROM sources ORDER BY id")
        )
    ).all()
    result: list[dict[str, Any]] = []
    for source in sources:
        mapping = source._mapping
        targets = (
            await session.execute(
                text(
                    """
SELECT id, target_ref, target_kind, enabled, cursor, last_polled_at, next_poll_at,
       poll_interval_s, error_count, last_error
  FROM source_targets WHERE source_id = :id ORDER BY id
"""
                ),
                {"id": mapping["id"]},
            )
        ).all()
        limit = rate_limits_for(mapping["kind"])
        rate_row = (
            await session.execute(
                text("SELECT bucket, window_start, used FROM rate_limits WHERE bucket = :bucket"),
                {"bucket": f"{mapping['kind']}:default"},
            )
        ).first()
        calls_row = (
            await session.execute(
                text(
                    "SELECT calls, cost_usd FROM source_calls_daily "
                    "WHERE source_id = :id AND day = (now() AT TIME ZONE 'UTC')::date"
                ),
                {"id": mapping["id"]},
            )
        ).first()
        result.append(
            {
                "id": mapping["id"],
                "kind": mapping["kind"],
                "name": mapping["name"],
                "enabled": mapping["enabled"],
                "config": mapping["config"] or {},
                "targets": [dict(t._mapping) for t in targets],
                "rate_limit": {
                    "bucket": rate_row._mapping["bucket"]
                    if rate_row
                    else f"{mapping['kind']}:default",
                    "window_start": rate_row._mapping["window_start"] if rate_row else None,
                    "used": rate_row._mapping["used"] if rate_row else None,
                    "limit": limit,
                },
                "calls_today": {
                    "calls": int(calls_row._mapping["calls"]) if calls_row else 0,
                    "cost_usd": float(calls_row._mapping["cost_usd"]) if calls_row else 0.0,
                },
            }
        )
    return result


async def create_target(
    session: AsyncSession,
    *,
    source_id: int,
    target_ref: str,
    target_kind: str,
    poll_interval_s: int | None,
) -> dict[str, Any] | None:
    """Crea un target; ``None`` se la sorgente non esiste, ``'conflict'`` se duplicato."""
    exists = await session.scalar(text("SELECT 1 FROM sources WHERE id = :id"), {"id": source_id})
    if not exists:
        return None
    try:
        row = (
            await session.execute(
                text(
                    """
INSERT INTO source_targets (source_id, target_ref, target_kind, poll_interval_s)
VALUES (:source_id, :target_ref, :target_kind, COALESCE(:poll_interval_s, 300))
RETURNING id, target_ref, target_kind, enabled, cursor, last_polled_at, next_poll_at,
          poll_interval_s, error_count, last_error
"""
                ),
                {
                    "source_id": source_id,
                    "target_ref": target_ref,
                    "target_kind": target_kind,
                    "poll_interval_s": poll_interval_s,
                },
            )
        ).first()
    except IntegrityError:
        return "conflict"  # type: ignore[return-value]
    return dict(row._mapping) if row else None


async def patch_target(
    session: AsyncSession,
    target_id: int,
    *,
    enabled: bool | None,
    poll_interval_s: int | None,
) -> dict[str, Any] | None:
    assignments: list[str] = []
    params: dict[str, Any] = {"id": target_id}
    if enabled is not None:
        assignments.append("enabled = :enabled")
        params["enabled"] = enabled
    if poll_interval_s is not None:
        assignments.append("poll_interval_s = :poll_interval_s")
        params["poll_interval_s"] = poll_interval_s
    if assignments:
        await session.execute(
            text(f"UPDATE source_targets SET {', '.join(assignments)} WHERE id = :id"), params
        )
    row = (
        await session.execute(
            text(
                """
SELECT id, target_ref, target_kind, enabled, cursor, last_polled_at, next_poll_at,
       poll_interval_s, error_count, last_error
  FROM source_targets WHERE id = :id
"""
            ),
            {"id": target_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def list_jobs(
    session: AsyncSession,
    *,
    state: str | None = None,
    topic: str | None = None,
    limit: int = 100,
) -> tuple[int, Any, list[dict[str, Any]]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if state is not None:
        where.append("state = :state")
        params["state"] = state
    if topic is not None:
        where.append("topic = :topic")
        params["topic"] = topic
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = await session.scalar(text(f"SELECT count(*) FROM jobs {clause}"), params)
    oldest = await session.scalar(text("SELECT min(run_after) FROM jobs WHERE state = 'pending'"))
    rows = (
        await session.execute(
            text(
                "SELECT id, topic, state, attempts, max_attempts, run_after, locked_by, "
                "last_error, created_at, finished_at, dedup_key FROM jobs "
                f"{clause} ORDER BY id DESC LIMIT :limit"
            ),
            {**params, "limit": limit},
        )
    ).all()
    return int(total or 0), oldest, [dict(r._mapping) for r in rows]


async def retry_job(session: AsyncSession, job_id: int) -> tuple[str, dict[str, Any] | None]:
    row = (
        await session.execute(text("SELECT id, state FROM jobs WHERE id = :id"), {"id": job_id})
    ).first()
    if row is None:
        return "not_found", None
    if row._mapping["state"] != "dead":
        return "not_dead", None
    updated = (
        await session.execute(
            text(
                """
UPDATE jobs
   SET state = 'pending', attempts = 0, run_after = now(), last_error = NULL,
       finished_at = NULL, locked_by = NULL, locked_at = NULL, heartbeat_at = NULL
 WHERE id = :id
RETURNING id, topic, state, attempts, max_attempts, run_after, locked_by, last_error,
          created_at, finished_at, dedup_key
"""
            ),
            {"id": job_id},
        )
    ).first()
    return "ok", dict(updated._mapping) if updated else None


async def stats(session: AsyncSession) -> dict[str, Any]:
    by_status = {
        row._mapping["status"]: int(row._mapping["n"])
        for row in (
            await session.execute(
                text("SELECT status, count(*) AS n FROM idea_clusters GROUP BY status")
            )
        ).all()
    }
    analyses = await session.scalar(text("SELECT count(*) FROM analyses"))
    watch_active = await session.scalar(text("SELECT count(*) FROM watchlist WHERE enabled"))
    llm_cost = (
        await session.execute(
            text(
                """
SELECT date_trunc('day', created_at AT TIME ZONE 'UTC') AS day, role, count(*) AS calls,
       sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out, sum(cost_usd) AS cost_usd
  FROM llm_calls
 WHERE created_at >= now() - interval '7 days'
 GROUP BY 1, 2 ORDER BY 1 DESC, 2
"""
            )
        )
    ).all()
    source_cost = (
        await session.execute(
            text(
                """
SELECT (day::timestamp AT TIME ZONE 'UTC') AS day, source_id, calls, cost_usd
  FROM source_calls_daily
 WHERE day >= ((now() AT TIME ZONE 'UTC')::date - 6)
 ORDER BY day DESC, source_id
"""
            )
        )
    ).all()
    queue = (
        await session.execute(
            text(
                "SELECT topic, state, jobs, oldest_pending, last_done, dead "
                "FROM v_queue_health ORDER BY topic, state"
            )
        )
    ).all()
    failed = (
        await session.execute(
            text(
                "SELECT id, role, model, status, error, created_at FROM llm_calls "
                "WHERE status <> 'ok' ORDER BY created_at DESC, id DESC LIMIT 20"
            )
        )
    ).all()
    return {
        "ideas": {
            "total": sum(by_status.values()),
            "new": by_status.get("new", 0),
            "analyzed": by_status.get("analyzed", 0),
            "watching": by_status.get("watching", 0),
            "rejected": by_status.get("rejected", 0),
            "archived": by_status.get("archived", 0),
        },
        "analyses": int(analyses or 0),
        "watch_active": int(watch_active or 0),
        "llm_cost_7d": [
            {
                "day": r._mapping["day"],
                "role": r._mapping["role"],
                "calls": int(r._mapping["calls"]),
                "tokens_in": int(r._mapping["tokens_in"] or 0),
                "tokens_out": int(r._mapping["tokens_out"] or 0),
                "cost_usd": float(r._mapping["cost_usd"] or 0),
            }
            for r in llm_cost
        ],
        "source_cost_7d": [
            {
                "day": r._mapping["day"],
                "source_id": r._mapping["source_id"],
                "calls": int(r._mapping["calls"]),
                "cost_usd": float(r._mapping["cost_usd"] or 0),
            }
            for r in source_cost
        ],
        "queue": [
            {
                "topic": r._mapping["topic"],
                "state": r._mapping["state"],
                "jobs": int(r._mapping["jobs"]),
                "oldest_pending": r._mapping["oldest_pending"],
                "last_done": r._mapping["last_done"],
                "dead": int(r._mapping["dead"]),
            }
            for r in queue
        ],
        "failed_llm_calls": [dict(r._mapping) for r in failed],
    }
