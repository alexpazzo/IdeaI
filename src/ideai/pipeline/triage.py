"""Handler ``pipeline.triage``: embedding, cancelli G1/G2 e tassonomia (§8.1, §8.2).

Ordine canonico: gli item ``new`` vengono embeddati; se un item ha un vicino a
similarità ``>= IDEAI_DUP_SIM_HIGH`` viene agganciato direttamente (G1) **senza**
alcuna chiamata LLM; gli altri formano batch per il modello di triage, che nella
fascia ambigua ``[LOW, HIGH)`` riceve i sommari canonici dei vicini e può emettere
``dup_of_idea_id`` (G2). La mappatura categoria→esito è l'unica fonte dello stato
dell'item (§8.1); gli item già lavorati non sono mai riconsiderati (idempotenza).
"""

from __future__ import annotations

from sqlalchemy import text

from ideai.domain import EmbeddingDimMismatch, ItemCategory, ItemState, RejectReason
from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext, finish
from ideai.pipeline.dedup import nearest_ideas, vec_literal
from ideai.prompts.loader import load_prompt
from ideai.prompts.schemas import TriageBatch, TriageItem

log = get_logger(__name__)

#: Dimensione dei chunk di embedding (§4.5).
_EMBED_CHUNK = 64

_ITEM_COLUMNS = "id, source_id, kind, external_id, title, body"

_LOAD_ITEMS_SQL = text(
    f"""
SELECT {_ITEM_COLUMNS} FROM items
 WHERE state = 'new' AND id = ANY(CAST(:ids AS bigint[]))
 ORDER BY id
"""
)

_LOAD_NEW_SQL = text(
    f"""
SELECT {_ITEM_COLUMNS} FROM items
 WHERE state = 'new'
 ORDER BY id
 LIMIT :n
"""
)

_UPSERT_EMBEDDING_SQL = text(
    """
INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec)
VALUES ('item', :owner_id, :model, :dim, CAST(:vec AS vector))
ON CONFLICT (owner_kind, owner_id, model)
DO UPDATE SET vec = EXCLUDED.vec, dim = EXCLUDED.dim
"""
)

_ATTACH_SQL = text("UPDATE items SET state = 'candidate', idea_id = :idea_id WHERE id = :item_id")

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

_RESULT_SQL = text(
    """
UPDATE items
   SET category = :category, triage_confidence = :confidence,
       tags = CAST(:tags AS text[]), state = :state, reject_reason = :reject_reason,
       idea_id = :idea_id, attempts = attempts + 1
 WHERE id = :item_id
"""
)

_SUMMARIES_SQL = text(
    """
SELECT id, canonical_summary FROM idea_clusters
 WHERE id = ANY(CAST(:ids AS bigint[]))
"""
)

#: Categorie che producono un'idea nuova (o un aggancio) piuttosto che un rifiuto.
_IDEA_CATEGORIES = (ItemCategory.PRODUCT_IDEA.value, ItemCategory.PAIN_POINT.value)


def _rejected_state(category: str, max_sim: float | None, settings) -> tuple[str, str | None]:
    """Mappatura categoria → esito (§8.1): unica fonte dello stato dell'item."""
    if category in _IDEA_CATEGORIES:
        return ItemState.TRIAGED.value, None
    if category == ItemCategory.MARKET_SIGNAL.value:
        if max_sim is not None and max_sim >= settings.dup_sim_low:
            return ItemState.TRIAGED.value, None
        return ItemState.REJECTED.value, RejectReason.NOT_IDEA.value
    if category == ItemCategory.SPAM.value:
        return ItemState.REJECTED.value, RejectReason.SPAM.value
    if category == ItemCategory.OFF_TOPIC.value:
        return ItemState.REJECTED.value, RejectReason.OFF_TOPIC.value
    # question, announcement
    return ItemState.REJECTED.value, RejectReason.NOT_IDEA.value


async def _embed_items(ctx: HandlerContext, session, items: list) -> dict[int, str]:
    """Embedda in chunk da 64 e upserta; ritorna ``{item_id: vettore letterale}``."""
    settings = ctx.settings
    texts: list[str] = []
    owners: list[int] = []
    for item in items:
        texts.append(f"{item['title'] or ''}\n{item['body']}")
        owners.append(int(item["id"]))

    embedded: dict[int, str] = {}
    for start in range(0, len(texts), _EMBED_CHUNK):
        chunk_texts = texts[start : start + _EMBED_CHUNK]
        chunk_owners = owners[start : start + _EMBED_CHUNK]
        vectors = await ctx.llm.embedder().embed(model=settings.embed_model, texts=chunk_texts)
        for owner, vector in zip(chunk_owners, vectors, strict=True):
            if not vector:
                continue
            if len(vector) != settings.embed_dim:
                raise EmbeddingDimMismatch(
                    f"item {owner}: embedding di {len(vector)} dimensioni, "
                    f"attese {settings.embed_dim} (IDEAI_EMBED_DIM)"
                )
            literal = vec_literal(vector)
            await session.execute(
                _UPSERT_EMBEDDING_SQL,
                {
                    "owner_id": owner,
                    "model": settings.embed_model,
                    "dim": len(vector),
                    "vec": literal,
                },
            )
            embedded[owner] = literal
    return embedded


async def _render_batch(
    ctx: HandlerContext,
    session,
    chunk: list,
    neighbours: dict[int, list[tuple[int, float]]],
    max_sim: dict[int, float],
) -> str:
    """Renderizza il segnaposto ``{batch}``: item numerati e, in fascia, i vicini."""
    settings = ctx.settings
    band_ids: set[int] = set()
    for item in chunk:
        sim = max_sim.get(int(item["id"]))
        if sim is not None and settings.dup_sim_low <= sim < settings.dup_sim_high:
            band_ids.update(idea_id for idea_id, _ in neighbours.get(int(item["id"]), []))

    summaries: dict[int, str] = {}
    if band_ids:
        rows = await session.execute(_SUMMARIES_SQL, {"ids": list(band_ids)})
        summaries = {int(row["id"]): row["canonical_summary"] for row in rows.mappings()}

    lines: list[str] = []
    for index, item in enumerate(chunk):
        head = f"{index}) [{item['kind']}]"
        if item["title"]:
            head += f" {item['title']}"
        lines.append(head)
        lines.append((item["body"] or "").strip())
        sim = max_sim.get(int(item["id"]))
        if sim is not None and settings.dup_sim_low <= sim < settings.dup_sim_high:
            descriptions = [
                f"{idea_id}: {summaries.get(idea_id, '')}"
                for idea_id, _ in neighbours.get(int(item["id"]), [])
            ]
            if descriptions:
                lines.append("Vicini canonici: " + "; ".join(descriptions))
    return "\n".join(lines)


def _aligned(batch: TriageBatch, expected: int) -> bool:
    if len(batch.results) != expected:
        return False
    return sorted(result.index for result in batch.results) == list(range(expected))


async def _run_chunk(
    ctx: HandlerContext,
    session,
    prompt,
    chunk: list,
    neighbours: dict[int, list[tuple[int, float]]],
    max_sim: dict[int, float],
) -> list[TriageItem]:
    """Una chiamata di triage sul chunk; se disallineata, ripete come chiamate singole."""
    settings = ctx.settings
    rendered = await _render_batch(ctx, session, chunk, neighbours, max_sim)
    result = await ctx.llm.triage().complete_json(
        model=settings.triage_model,
        system=prompt.system,
        user=prompt.render_user(batch=rendered),
        schema=TriageBatch.model_json_schema(),
        max_tokens=prompt.max_tokens,
        temperature=prompt.temperature,
    )
    batch = TriageBatch.model_validate(result.content)
    if _aligned(batch, len(chunk)):
        return list(batch.results)

    singles: list[TriageItem] = []
    for item in chunk:
        rendered_one = await _render_batch(ctx, session, [item], neighbours, max_sim)
        single = await ctx.llm.triage().complete_json(
            model=settings.triage_model,
            system=prompt.system,
            user=prompt.render_user(batch=rendered_one),
            schema=TriageBatch.model_json_schema(),
            max_tokens=prompt.max_tokens,
            temperature=prompt.temperature,
        )
        singles.append(TriageBatch.model_validate(single.content).results[0])
    return singles


async def handle_triage(job, ctx: HandlerContext) -> None:
    """Classifica gli item ``new`` e li instrada verso aggancio, cluster o rifiuto."""
    settings = ctx.settings
    payload = job.payload or {}

    async with ctx.db() as session:
        if payload.get("item_ids"):
            items = list(
                (
                    await session.execute(
                        _LOAD_ITEMS_SQL,
                        {"ids": [int(i) for i in payload["item_ids"]]},
                    )
                ).mappings()
            )
        else:
            limit = int(payload.get("batch") or settings.triage_batch)
            items = list((await session.execute(_LOAD_NEW_SQL, {"n": limit})).mappings())

        if not items:
            await finish(session, job.id)
            await session.commit()
            return

        embeddable = [item for item in items if f"{item['title'] or ''}\n{item['body']}".strip()]
        embedded = await _embed_items(ctx, session, embeddable)

        neighbours: dict[int, list[tuple[int, float]]] = {}
        max_sim: dict[int, float] = {}
        for item in embeddable:
            item_id = int(item["id"])
            if item_id not in embedded:
                continue
            near = await nearest_ideas(session, embedded[item_id], settings.embed_model, 5)
            neighbours[item_id] = near
            if near:
                max_sim[item_id] = near[0][1]

        prompt = load_prompt("triage", "v1")
        pending: list = []
        for item in items:
            item_id = int(item["id"])
            sim = max_sim.get(item_id)
            if sim is not None and sim >= settings.dup_sim_high:
                idea_id, similarity = neighbours[item_id][0]
                role = "evidence" if item["kind"] == "post" else "comment"
                await session.execute(_ATTACH_SQL, {"idea_id": idea_id, "item_id": item_id})
                await session.execute(
                    _IDEA_ITEM_SQL,
                    {
                        "idea_id": idea_id,
                        "item_id": item_id,
                        "role": role,
                        "similarity": similarity,
                    },
                )
                await session.execute(
                    _IDEA_UPDATE_SQL,
                    {
                        "idea_id": idea_id,
                        "item_id": item_id,
                        "kind": "new_post" if item["kind"] == "post" else "new_comment",
                        "summary": f"Nuova evidenza agganciata (similarità {similarity:.2f})",
                    },
                )
                await ctx.queue.enqueue("pipeline.score", {"idea_id": idea_id})
            else:
                pending.append(item)

        for start in range(0, len(pending), settings.triage_batch):
            chunk = pending[start : start + settings.triage_batch]
            results = await _run_chunk(ctx, session, prompt, chunk, neighbours, max_sim)
            for item, result in zip(chunk, results, strict=True):
                item_id = int(item["id"])
                sim = max_sim.get(item_id)
                state, reason = _rejected_state(result.category.value, sim, settings)
                await session.execute(
                    _RESULT_SQL,
                    {
                        "item_id": item_id,
                        "category": result.category.value,
                        "confidence": result.confidence,
                        "tags": list(result.tags),
                        "state": state,
                        "reject_reason": reason,
                        "idea_id": None,
                    },
                )
                if state == ItemState.TRIAGED.value:
                    await ctx.queue.enqueue(
                        "pipeline.cluster",
                        {
                            "item_id": item_id,
                            "dup_of_idea_id": result.dup_of_idea_id,
                            "similarity": sim,
                            "one_line_summary": result.one_line_summary,
                        },
                    )

        await finish(session, job.id)
        await session.commit()
        log.info("triage concluso", job_id=job.id, item=len(items), batch=len(pending))


__all__ = ["handle_triage"]
