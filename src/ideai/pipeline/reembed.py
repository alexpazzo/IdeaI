"""Handler ``maintenance.reembed``: ricalcolo degli embedding (§4.5).

Seleziona gli owner (tutte le idee, oppure una sola; con ``all`` anche gli item non
``rejected``/``archived``), li embedda in chunk da 64 con il modello corrente e
upserta. Al termine ``REINDEX INDEX embeddings_idea_vec_idx`` in una transazione
dedicata (``REINDEX INDEX`` non ``CONCURRENTLY``: ammesso in transazione). I vettori
di modelli diversi restano in tabella ed esclusi dalle query dal filtro ``model``.
"""

from __future__ import annotations

from sqlalchemy import text

from ideai.domain import EmbeddingDimMismatch
from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext, finish
from ideai.pipeline.dedup import vec_literal

log = get_logger(__name__)

_CHUNK = 64

_IDEAS_ALL_SQL = text("SELECT id, title, canonical_summary FROM idea_clusters")

_IDEAS_ACTIVE_SQL = text(
    "SELECT id, title, canonical_summary FROM idea_clusters WHERE status <> 'archived'"
)

_IDEA_ONE_SQL = text("SELECT id, title, canonical_summary FROM idea_clusters WHERE id = :idea_id")

_ITEMS_SQL = text(
    """
SELECT id, title, body FROM items
 WHERE state NOT IN ('rejected', 'archived')
"""
)

_UPSERT_SQL = text(
    """
INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec)
VALUES (:owner_kind, :owner_id, :model, :dim, CAST(:vec AS vector))
ON CONFLICT (owner_kind, owner_id, model)
DO UPDATE SET vec = EXCLUDED.vec, dim = EXCLUDED.dim
"""
)

_REINDEX_SQL = text("REINDEX INDEX embeddings_idea_vec_idx")


async def handle_reembed(job, ctx: HandlerContext) -> None:
    """Ricalcola gli embedding nello scope richiesto e ricostruisce l'indice HNSW."""
    settings = ctx.settings
    payload = job.payload or {}
    scope = payload.get("scope", "all")
    idea_id = payload.get("idea_id")

    async with ctx.db() as session:
        owners: list[tuple[str, int, str]] = []
        if scope == "idea" and idea_id is not None:
            rows = await session.execute(_IDEA_ONE_SQL, {"idea_id": int(idea_id)})
        elif scope == "idea":
            rows = await session.execute(_IDEAS_ACTIVE_SQL)
        else:
            rows = await session.execute(_IDEAS_ALL_SQL)
        for row in rows.mappings():
            owners.append(("idea", int(row["id"]), f"{row['title']}\n{row['canonical_summary']}"))

        if scope == "all":
            items = await session.execute(_ITEMS_SQL)
            for row in items.mappings():
                owners.append(("item", int(row["id"]), f"{row['title'] or ''}\n{row['body']}"))

        for start in range(0, len(owners), _CHUNK):
            chunk = owners[start : start + _CHUNK]
            vectors = await ctx.llm.embedder().embed(
                model=settings.embed_model, texts=[text_ for _, _, text_ in chunk]
            )
            for (owner_kind, owner_id, _), vector in zip(chunk, vectors, strict=True):
                if not vector:
                    continue
                if len(vector) != settings.embed_dim:
                    raise EmbeddingDimMismatch(
                        f"{owner_kind} {owner_id}: embedding di {len(vector)} dimensioni, "
                        f"attese {settings.embed_dim} (IDEAI_EMBED_DIM)"
                    )
                await session.execute(
                    _UPSERT_SQL,
                    {
                        "owner_kind": owner_kind,
                        "owner_id": owner_id,
                        "model": settings.embed_model,
                        "dim": len(vector),
                        "vec": vec_literal(vector),
                    },
                )

        await finish(session, job.id)
        await session.commit()

    async with ctx.db.begin() as conn:
        await conn.execute(_REINDEX_SQL)

    log.info("reembed concluso", job_id=job.id, scope=scope, owner=len(owners))


__all__ = ["handle_reembed"]
