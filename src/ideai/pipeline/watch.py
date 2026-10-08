"""Handler ``pipeline.watch``: watch adattivo di un thread (§5.5, §8.5).

Il watch è un percorso a sé: **non** passa da G0/G1/G2, **non** crea mai idee e si
limita ad aggiornare le metriche del seed, inserire i commenti nuovi come evidenze
``update`` e accodare una ri-analisi quando la variazione supera ``IDEAI_WATCH_MIN_DELTA``
(o, alla prima passata, quando esiste almeno un commento). L'intervallo raddoppia
senza novità e si riazzera a ``IDEAI_WATCH_MIN_INTERVAL_S`` quando ce ne sono.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import text

from ideai.domain import ExternalRef
from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext, finish

log = get_logger(__name__)

_LOAD_WATCH_SQL = text(
    """
SELECT idea_id, enabled, interval_s, mode, last_checked_at, last_change_at, added_by
  FROM watchlist
 WHERE idea_id = :idea_id
"""
)

_SEED_SQL = text(
    """
SELECT i.id, i.external_id, i.kind, i.score, i.num_comments, i.source_id,
       i.thread_external_id, s.kind AS source_kind
  FROM idea_items ii
  JOIN items i ON i.id = ii.item_id
  JOIN sources s ON s.id = i.source_id
 WHERE ii.idea_id = :idea_id AND ii.role = 'seed'
 ORDER BY ii.added_at
 LIMIT 1
"""
)

_ITEM_SQL = text(
    """
SELECT id, content_hash FROM items
 WHERE source_id = :source_id AND external_id = :external_id
"""
)

_REFRESH_SQL = text(
    """
UPDATE items
   SET score = :score, num_comments = :num_comments, edited_at = :edited_at,
       fetched_at = now(),
       body = CASE WHEN :changed THEN :body ELSE body END,
       content_hash = CASE WHEN :changed THEN :content_hash ELSE content_hash END
 WHERE id = :item_id
"""
)

_INSERT_ITEM_SQL = text(
    """
INSERT INTO items (source_id, external_id, kind, thread_external_id, parent_external_id,
                   title, body, author_hash, url, score, num_comments, lang,
                   created_at, edited_at, fetched_at, content_hash, raw, state, idea_id)
VALUES (:source_id, :external_id, :kind, :thread_external_id, :parent_external_id,
        :title, :body, :author_hash, :url, :score, :num_comments, :lang,
        :created_at, :edited_at, now(), :content_hash, CAST(:raw AS jsonb),
        'candidate', :idea_id)
ON CONFLICT (source_id, external_id) DO NOTHING
RETURNING id
"""
)

_IDEA_ITEM_SQL = text(
    """
INSERT INTO idea_items (idea_id, item_id, role, similarity)
VALUES (:idea_id, :item_id, 'update', NULL)
ON CONFLICT (idea_id, item_id) DO NOTHING
"""
)

_IDEA_UPDATE_SQL = text(
    """
INSERT INTO idea_updates (idea_id, item_id, kind, summary)
VALUES (:idea_id, :item_id, :kind, :summary)
"""
)

_RECOUNT_SQL = text(
    """
UPDATE idea_clusters
   SET last_activity_at = now(),
       item_count = (SELECT count(*) FROM idea_items WHERE idea_id = :idea_id),
       source_kinds = COALESCE((SELECT array_agg(DISTINCT s.kind)
                                  FROM idea_items ii
                                  JOIN items i ON i.id = ii.item_id
                                  JOIN sources s ON s.id = i.source_id
                                 WHERE ii.idea_id = :idea_id), '{}')
 WHERE id = :idea_id
"""
)

_UPDATE_WATCH_SQL = text(
    """
UPDATE watchlist
   SET interval_s = :interval_s, last_checked_at = :last_checked, last_change_at = :last_change,
       enabled = :enabled
 WHERE idea_id = :idea_id
"""
)

_SET_STATUS_SQL = text("UPDATE idea_clusters SET status = :status WHERE id = :idea_id")


def _grew(new: int | None, old: int | None) -> bool:
    """Variazione oltre il 20% rispetto al valore persistito (§8.5)."""
    if new is None or old is None or old <= 0:
        return False
    return new > old * 1.2


async def handle_watch(job, ctx: HandlerContext) -> None:
    """Legge il thread, registra le novità e accorda intervallo e ri-analisi."""
    settings = ctx.settings
    idea_id = int(job.payload["idea_id"])
    initial = bool(job.payload.get("initial"))

    async with ctx.db() as session:
        watch = (
            (await session.execute(_LOAD_WATCH_SQL, {"idea_id": idea_id})).mappings().one_or_none()
        )
        # Il watch *iniziale* non richiede una riga in `watchlist`: quella la crea
        # `pipeline.score` quando l'idea supera la soglia (§8.5). Qui la riga serve solo
        # al watch ricorrente, che senza riga non ha né intervallo né stato da aggiornare.
        if watch is None and not initial:
            await finish(session, job.id)
            await session.commit()
            return

        seed = (await session.execute(_SEED_SQL, {"idea_id": idea_id})).mappings().one_or_none()
        if seed is None or not seed["thread_external_id"]:
            await finish(session, job.id)
            await session.commit()
            return

        adapter = ctx.adapters[seed["source_kind"]]
        reference = ExternalRef(seed["source_kind"], seed["thread_external_id"])
        since = None if initial else (watch["last_checked_at"] if watch is not None else None)
        page = await adapter.fetch_thread(reference, since)

        now = ctx.now()
        source_id = int(seed["source_id"])
        seed_external = seed["external_id"]
        old_score = seed["score"]
        old_comments = seed["num_comments"]
        refresh_seed = (
            bool(initial)
            or (watch["mode"] if watch is not None else "thread_full") == "thread_full"
        )

        new_count = 0
        new_comments = 0
        seed_changed = False

        for item in page.items:
            existing = (
                (
                    await session.execute(
                        _ITEM_SQL, {"source_id": source_id, "external_id": item.external_id}
                    )
                )
                .mappings()
                .one_or_none()
            )

            if existing is not None:
                if item.external_id == seed_external and refresh_seed:
                    changed = existing["content_hash"] != item.content_hash
                    await session.execute(
                        _REFRESH_SQL,
                        {
                            "item_id": int(existing["id"]),
                            "score": item.score,
                            "num_comments": item.num_comments,
                            "edited_at": item.edited_at,
                            "changed": changed,
                            "body": item.body,
                            "content_hash": item.content_hash,
                        },
                    )
                    if changed:
                        await session.execute(
                            _IDEA_UPDATE_SQL,
                            {
                                "idea_id": idea_id,
                                "item_id": int(existing["id"]),
                                "kind": "edit",
                                "summary": "Il contenuto di origine è stato modificato",
                            },
                        )
                    if _grew(item.score, old_score) or _grew(item.num_comments, old_comments):
                        seed_changed = True
                    if changed:
                        seed_changed = True
                continue

            new_id = await session.scalar(
                _INSERT_ITEM_SQL,
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
                    "idea_id": idea_id,
                },
            )
            if new_id is None:
                continue
            new_count += 1
            if item.kind == "comment":
                new_comments += 1
            await session.execute(_IDEA_ITEM_SQL, {"idea_id": idea_id, "item_id": int(new_id)})
            await session.execute(
                _IDEA_UPDATE_SQL,
                {
                    "idea_id": idea_id,
                    "item_id": int(new_id),
                    "kind": "new_post" if item.kind == "post" else "new_comment",
                    "summary": (
                        f"{new_count} nuovi elementi nel thread"
                        if item.kind == "post"
                        else f"{new_comments} nuovi commenti"
                    ),
                },
            )

        if new_count:
            await session.execute(_RECOUNT_SQL, {"idea_id": idea_id})

        trigger = (
            new_count >= settings.watch_min_delta or (initial and new_comments >= 1) or seed_changed
        )
        if trigger:
            await ctx.queue.enqueue(
                "pipeline.analyze",
                {"idea_id": idea_id, "reason": "initial" if initial else "watch_delta"},
            )

        # Senza riga di `watchlist` (watch iniziale di un'idea non ancora sopra soglia)
        # non c'è stato da aggiornare: né intervallo, né scadenza, né stato dell'idea.
        interval_s = None
        expired = False
        if watch is not None:
            changed_now = new_count > 0 or seed_changed
            if changed_now:
                interval_s = settings.watch_min_interval_s
                last_change = now
            else:
                interval_s = min(settings.watch_max_interval_s, max(1, watch["interval_s"]) * 2)
                last_change = watch["last_change_at"]

            if last_change is not None:
                expired = last_change < now - timedelta(days=settings.watch_expire_days)

            await session.execute(
                _UPDATE_WATCH_SQL,
                {
                    "idea_id": idea_id,
                    "interval_s": interval_s,
                    "last_checked": now,
                    "last_change": last_change,
                    "enabled": not expired,
                },
            )
            if expired:
                await session.execute(
                    _IDEA_UPDATE_SQL,
                    {
                        "idea_id": idea_id,
                        "item_id": None,
                        "kind": "watch_paused",
                        "summary": (
                            f"Watch sospeso: nessuna novità da {settings.watch_expire_days} giorni"
                        ),
                    },
                )
                await session.execute(_SET_STATUS_SQL, {"status": "analyzed", "idea_id": idea_id})

        await finish(session, job.id)
        await session.commit()
        log.info(
            "watch concluso",
            job_id=job.id,
            idea_id=idea_id,
            nuovi=new_count,
            intervallo=interval_s,
            sospeso=expired,
        )


__all__ = ["handle_watch"]
