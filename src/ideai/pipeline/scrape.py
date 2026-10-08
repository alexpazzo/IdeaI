"""Handler ``pipeline.scrape``: ingestione going-forward di un target (§5.2, §5.9).

Il cursore di ``source_targets`` avanza **solo qui**; ``next_poll_at`` è gestito
esclusivamente dallo scheduler. Il cancello G0 (duplicato esatto) si applica ai
soli insert nuovi: per una riga ``(source_id, external_id)`` già esistente si fa
solo upsert delle metriche e lo ``state`` resta invariato (divergenza 4 del piano).
"""

from __future__ import annotations

import json

from sqlalchemy import text

from ideai.domain import (
    NonRetryableError,
    NormalizedItem,
    SourceTarget,
    SourceUnavailable,
)
from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext, finish
from ideai.runtime.ratelimit import record_source_call

log = get_logger(__name__)

#: Corpi che Reddit usa per contenuti rimossi/cancellati.
_REMOVED_BODIES = ("[removed]", "[deleted]")

_LOAD_TARGET_SQL = text(
    """
SELECT t.id, t.source_id, t.target_ref, t.target_kind, t.cursor, t.poll_interval_s,
       t.enabled AS target_enabled,
       s.kind AS source_kind, s.config AS source_config, s.enabled AS source_enabled
  FROM source_targets t
  JOIN sources s ON s.id = t.source_id
 WHERE t.id = :target_id
"""
)

_GET_ITEM_SQL = text(
    """
SELECT id, content_hash, state, idea_id
  FROM items
 WHERE source_id = :source_id AND external_id = :external_id
"""
)

_G0_SQL = text(
    """
SELECT 1 FROM items
 WHERE content_hash = :content_hash
   AND fetched_at >= now() - make_interval(days => :g0_window_days)
 LIMIT 1
"""
)

# Upsert di §5.2: ``state``, ``category``, ``idea_id`` e ``reject_reason`` non vengono
# mai toccati in conflitto; ``raw`` non viene ripopolato per gli item già lavorati.
_UPSERT_SQL = text(
    """
INSERT INTO items (source_id, external_id, kind, thread_external_id, parent_external_id,
                   title, body, author_hash, url, score, num_comments, lang,
                   created_at, edited_at, fetched_at, content_hash, raw, state, reject_reason)
VALUES (:source_id, :external_id, :kind, :thread_external_id, :parent_external_id,
        :title, :body, :author_hash, :url, :score, :num_comments, :lang,
        :created_at, :edited_at, now(), :content_hash, CAST(:raw AS jsonb),
        :state, :reject_reason)
ON CONFLICT (source_id, external_id) DO UPDATE SET
    score = EXCLUDED.score,
    num_comments = EXCLUDED.num_comments,
    edited_at = EXCLUDED.edited_at,
    fetched_at = now(),
    body = CASE WHEN items.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                THEN EXCLUDED.body ELSE items.body END,
    content_hash = CASE WHEN items.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                        THEN EXCLUDED.content_hash ELSE items.content_hash END,
    raw = CASE WHEN items.state IN ('candidate', 'analyzed')
               THEN items.raw ELSE EXCLUDED.raw END
RETURNING id
"""
)

_ARCHIVE_REMOVED_SQL = text(
    """
UPDATE items SET state = 'archived', reject_reason = 'removed'
 WHERE id = :item_id AND state <> 'archived'
"""
)

_IDEA_UPDATE_SQL = text(
    """
INSERT INTO idea_updates (idea_id, item_id, kind, summary)
VALUES (:idea_id, :item_id, :kind, :summary)
"""
)

_TARGET_CURSOR_SQL = text(
    """
UPDATE source_targets
   SET cursor = CAST(:cursor AS jsonb), last_polled_at = now(),
       error_count = 0, last_error = NULL
 WHERE id = :target_id
"""
)

_TARGET_TOUCH_SQL = text(
    """
UPDATE source_targets
   SET last_polled_at = now(), error_count = 0, last_error = NULL
 WHERE id = :target_id
"""
)

_SOURCE_ERROR_SQL = text(
    """
UPDATE source_targets
   SET error_count = error_count + 1,
       last_error = :last_error,
       enabled = CASE WHEN :disable AND error_count + 1 >= 10 THEN false ELSE enabled END
 WHERE id = :target_id
"""
)


def _removed(body: str) -> bool:
    return body.strip() in _REMOVED_BODIES


async def _record_source_error(session, target_id: int, error: str, *, disable: bool) -> None:
    """§5.9: incrementa ``error_count`` e, oltre 10 errori non ritentabili, disabilita."""
    await session.execute(
        _SOURCE_ERROR_SQL,
        {"target_id": target_id, "last_error": error, "disable": disable},
    )


def _item_params(source_id: int, item: NormalizedItem, state: str, reason: str | None) -> dict:
    return {
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
        "state": state,
        "reject_reason": reason,
    }


async def handle_scrape(job, ctx: HandlerContext) -> None:
    """Legge una pagina dal target, la upserta e accoda il triage degli item nuovi."""
    settings = ctx.settings
    target_id = int(job.payload["target_id"])

    async with ctx.db() as session:
        target = (
            (await session.execute(_LOAD_TARGET_SQL, {"target_id": target_id}))
            .mappings()
            .one_or_none()
        )
        if target is None:
            await finish(session, job.id)
            await session.commit()
            return
        if not target["target_enabled"] or not target["source_enabled"]:
            await finish(session, job.id)
            await session.commit()
            return

        source_id = int(target["source_id"])
        source_kind = target["source_kind"]
        adapter = ctx.adapters[source_kind]
        policy = adapter.rate_policy()

        # Passo breve separato: rate limit e contabilizzazione della chiamata (§6.7, §5.9).
        await ctx.rate.acquire(
            f"{source_kind}:default", policy.requests_per_minute, settings.rate_limit_window_s
        )
        cost = float((target["source_config"] or {}).get("cost_per_call_usd", 0.0))
        async with ctx.db.begin() as conn:
            await record_source_call(conn, source_id, cost)

        source_target = SourceTarget(
            id=int(target["id"]),
            source_id=source_id,
            target_ref=target["target_ref"],
            target_kind=target["target_kind"],
            cursor=target["cursor"],
        )

        try:
            page = await adapter.fetch_new(
                source_target, target["cursor"], settings.scrape_page_size
            )
        except SourceUnavailable as exc:
            await _record_source_error(session, target_id, repr(exc), disable=False)
            await session.commit()
            raise
        except NonRetryableError as exc:
            await _record_source_error(session, target_id, repr(exc), disable=True)
            await session.commit()
            raise

        new_item_ids: list[int] = []
        for item in page.items:
            existing = (
                (
                    await session.execute(
                        _GET_ITEM_SQL,
                        {"source_id": source_id, "external_id": item.external_id},
                    )
                )
                .mappings()
                .one_or_none()
            )

            if existing is None:
                duplicate = await session.scalar(
                    _G0_SQL,
                    {
                        "content_hash": item.content_hash,
                        "g0_window_days": settings.g0_window_days,
                    },
                )
                state = "rejected" if duplicate is not None else "new"
                reason = "exact_dup" if duplicate is not None else None
                new_id = await session.scalar(
                    _UPSERT_SQL, _item_params(source_id, item, state, reason)
                )
                if state == "new":
                    new_item_ids.append(int(new_id))
                continue

            changed = existing["content_hash"] != item.content_hash
            await session.execute(
                _UPSERT_SQL,
                _item_params(source_id, item, existing["state"], None),
            )
            if changed and existing["idea_id"] is not None:
                if _removed(item.body):
                    await session.execute(_ARCHIVE_REMOVED_SQL, {"item_id": int(existing["id"])})
                await session.execute(
                    _IDEA_UPDATE_SQL,
                    {
                        "idea_id": int(existing["idea_id"]),
                        "item_id": int(existing["id"]),
                        "kind": "edit",
                        "summary": "Il contenuto di origine è stato modificato",
                    },
                )

        if page.next_cursor is not None:
            await session.execute(
                _TARGET_CURSOR_SQL,
                {"target_id": target_id, "cursor": json.dumps(page.next_cursor)},
            )
        else:
            await session.execute(_TARGET_TOUCH_SQL, {"target_id": target_id})

        if new_item_ids:
            await ctx.queue.enqueue("pipeline.triage", {"item_ids": new_item_ids})

        await finish(session, job.id)
        await session.commit()
        log.info(
            "scrape concluso",
            job_id=job.id,
            target_id=target_id,
            items=len(page.items),
            nuovi=len(new_item_ids),
        )


__all__ = ["handle_scrape"]
