"""Handler ``pipeline.cluster``: aggancio a un'idea o creazione di una nuova (§8.2).

Riceve dal triage il verdetto di deduplica G2 (``dup_of_idea_id``) e la similarità
massima: se c'è un aggancio — esplicito o per ``market_signal`` con un vicino — la
riga va in ``idea_items`` con ruolo ``evidence``/``comment``; altrimenti (solo
``product_idea``/``pain_point``) nasce una nuova idea con l'item come ``seed``.
"""

from __future__ import annotations

from sqlalchemy import text

from ideai.domain import EmbeddingDimMismatch
from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext, finish
from ideai.pipeline.dedup import nearest_ideas, vec_literal

log = get_logger(__name__)

_LOAD_ITEM_SQL = text(
    """
SELECT i.id, i.source_id, i.kind, i.external_id, i.title, i.body, i.category, i.idea_id,
       s.kind AS source_kind
  FROM items i
  JOIN sources s ON s.id = i.source_id
 WHERE i.id = :item_id
"""
)

_ITEM_VEC_SQL = text(
    """
SELECT CAST(vec AS text) AS vec_text FROM embeddings
 WHERE owner_kind = 'item' AND owner_id = :item_id AND model = :model
"""
)

_ATTACH_ITEM_SQL = text(
    "UPDATE items SET state = 'candidate', idea_id = :idea_id WHERE id = :item_id"
)

_REJECT_ITEM_SQL = text(
    "UPDATE items SET state = 'rejected', reject_reason = 'not_idea' WHERE id = :item_id"
)

_IDEA_ITEM_SQL = text(
    """
INSERT INTO idea_items (idea_id, item_id, role, similarity)
VALUES (:idea_id, :item_id, :role, :similarity)
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

_INSERT_IDEA_SQL = text(
    """
INSERT INTO idea_clusters (title, canonical_summary, status, first_seen_at, last_activity_at)
VALUES (:title, :summary, 'new', now(), now())
RETURNING id
"""
)

_UPSERT_IDEA_EMBEDDING_SQL = text(
    """
INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec)
VALUES ('idea', :owner_id, :model, :dim, CAST(:vec AS vector))
ON CONFLICT (owner_kind, owner_id, model)
DO UPDATE SET vec = EXCLUDED.vec, dim = EXCLUDED.dim
"""
)


async def _attach(ctx: HandlerContext, session, idea_id: int, item, similarity) -> None:
    """Aggancia l'item a un'idea esistente e accoda il ricalcolo dello score."""
    role = "evidence" if item["kind"] == "post" else "comment"
    await session.execute(_ATTACH_ITEM_SQL, {"idea_id": idea_id, "item_id": int(item["id"])})
    await session.execute(
        _IDEA_ITEM_SQL,
        {
            "idea_id": idea_id,
            "item_id": int(item["id"]),
            "role": role,
            "similarity": similarity,
        },
    )
    await session.execute(
        _IDEA_UPDATE_SQL,
        {
            "idea_id": idea_id,
            "item_id": int(item["id"]),
            "kind": "new_post" if item["kind"] == "post" else "new_comment",
            "summary": "Nuova evidenza agganciata",
        },
    )
    await session.execute(_RECOUNT_SQL, {"idea_id": idea_id})
    await ctx.queue.enqueue("pipeline.score", {"idea_id": idea_id})


async def _resolve_market_signal(ctx: HandlerContext, session, item) -> tuple[int, float] | None:
    """Per un ``market_signal`` senza ``dup_of_idea_id`` risolve il vicino dall'embedding."""
    settings = ctx.settings
    vector = await session.scalar(
        _ITEM_VEC_SQL, {"item_id": int(item["id"]), "model": settings.embed_model}
    )
    if not vector:
        return None
    near = await nearest_ideas(session, vector, settings.embed_model, 1)
    return near[0] if near else None


async def _create_idea(ctx: HandlerContext, session, item, summary_hint: str | None = None) -> int:
    """Crea l'idea con l'item come seed, ne calcola l'embedding e accoda l'analisi."""
    settings = ctx.settings
    body = item["body"] or ""
    title = item["title"] or body[:120]
    # Il sommario canonico è quello del triage (§8.2): è il testo su cui G1/G2
    # confrontano l'idea, quindi non deve essere un troncamento grezzo del corpo.
    summary = (summary_hint or "").strip() or body[:300]
    idea_id = int(await session.scalar(_INSERT_IDEA_SQL, {"title": title, "summary": summary}))
    await session.execute(
        _IDEA_ITEM_SQL,
        {"idea_id": idea_id, "item_id": int(item["id"]), "role": "seed", "similarity": None},
    )
    await session.execute(_ATTACH_ITEM_SQL, {"idea_id": idea_id, "item_id": int(item["id"])})

    vectors = await ctx.llm.embedder().embed(
        model=settings.embed_model, texts=[f"{title}\n{summary}"]
    )
    vector = vectors[0]
    if len(vector) != settings.embed_dim:
        raise EmbeddingDimMismatch(
            f"idea {idea_id}: embedding di {len(vector)} dimensioni, "
            f"attese {settings.embed_dim} (IDEAI_EMBED_DIM)"
        )
    await session.execute(
        _UPSERT_IDEA_EMBEDDING_SQL,
        {
            "owner_id": idea_id,
            "model": settings.embed_model,
            "dim": len(vector),
            "vec": vec_literal(vector),
        },
    )

    adapter = ctx.adapters[item["source_kind"]]
    if item["kind"] == "post" and adapter.capabilities().has_comments:
        await ctx.queue.enqueue(
            "pipeline.watch", {"idea_id": idea_id, "initial": True}, priority=50
        )
    else:
        await ctx.queue.enqueue("pipeline.analyze", {"idea_id": idea_id, "reason": "initial"})
    return idea_id


async def handle_cluster(job, ctx: HandlerContext) -> None:
    """Instrada un item triagiato verso un'idea esistente o una nuova."""
    payload = job.payload or {}
    item_id = int(payload["item_id"])
    dup_of_idea_id = payload.get("dup_of_idea_id")
    similarity = payload.get("similarity")

    async with ctx.db() as session:
        item = (
            (await session.execute(_LOAD_ITEM_SQL, {"item_id": item_id})).mappings().one_or_none()
        )
        if item is None:
            await finish(session, job.id)
            await session.commit()
            return

        if dup_of_idea_id is not None:
            await _attach(ctx, session, int(dup_of_idea_id), item, similarity)
        elif item["category"] == "market_signal":
            resolved = await _resolve_market_signal(ctx, session, item)
            if resolved is None:
                await session.execute(_REJECT_ITEM_SQL, {"item_id": item_id})
            else:
                idea_id, sim = resolved
                await _attach(ctx, session, idea_id, item, sim)
        else:
            await _create_idea(ctx, session, item, payload.get("one_line_summary"))

        await finish(session, job.id)
        await session.commit()
        log.info("cluster concluso", job_id=job.id, item_id=item_id)


__all__ = ["handle_cluster"]
