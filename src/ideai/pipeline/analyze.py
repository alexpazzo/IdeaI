"""Handler ``pipeline.analyze``: una revisione della valutazione di un'idea (§8.3).

Carica idea ed evidenze (post e commenti, con i commenti che esprimono domanda
anteposti), chiama l'analista, valida e tronca le citazioni, scrive la nuova
revisione supersedendo la precedente, rigenera titolo/sommario canonico con il
modello di triage, ricalcola l'embedding dell'idea, azzera ``raw`` degli item
coinvolti e accoda lo score. La nuova revisione è ``max(revision) + 1``.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from ideai.domain import LlmRole, Verdict
from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext, finish
from ideai.pipeline.dedup import vec_literal
from ideai.prompts.loader import load_prompt
from ideai.prompts.schemas import AnalysisPayload, EvidenceQuote
from ideai.scoring import ScoreInputs, opportunity_score

log = get_logger(__name__)

#: Frasi che segnalano domanda/attenzione all'acquisto: i commenti che le contengono
#: vengono anteposti nella lista delle evidenze (§8.3).
_DEMAND_PHRASES = (
    "pago per",
    "pay for",
    "esiste qualcosa",
    "is there anything",
    "come risolvo",
    "how do i",
)
_DEMAND_COND = " OR ".join(f"lower(i.body) LIKE :demand_{i}" for i in range(len(_DEMAND_PHRASES)))
_DEMAND_PARAMS = {f"demand_{i}": f"%{phrase}%" for i, phrase in enumerate(_DEMAND_PHRASES)}

#: Prompt ad hoc per la rigenerazione del sommario: hash proprio, validato da schema.
_SUMMARY_PROMPT_HASH = hashlib.sha256(b"ideai.summarize.v1").hexdigest()
_SUMMARY_SYSTEM = (
    "Sei l'editor di IdeaI. Rispondi esclusivamente con un oggetto JSON "
    '{"title": string, "canonical_summary": string} in italiano, senza testo aggiuntivo.'
)


class _Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    canonical_summary: str


_SUMMARY_SCHEMA = _Summary.model_json_schema()

_IDEA_SQL = text(
    "SELECT id, title, canonical_summary, status FROM idea_clusters WHERE id = :idea_id"
)

_EVIDENCE_SQL = text(
    f"""
SELECT i.id, i.external_id, i.kind, i.title, i.body, i.score, i.num_comments,
       ii.role, ii.similarity, ii.added_at
  FROM idea_items ii
  JOIN items i ON i.id = ii.item_id
 WHERE ii.idea_id = :idea_id
 ORDER BY (CASE WHEN i.kind = 'comment' AND ({_DEMAND_COND}) THEN 0 ELSE 1 END),
          (COALESCE(i.score, 0) + 2 * COALESCE(i.num_comments, 0)) DESC,
          i.id
 LIMIT :limit
"""
)

_SEED_SQL = text(
    """
SELECT i.id, i.external_id, i.kind, i.title, i.body, i.score, i.num_comments, i.source_id
  FROM idea_items ii
  JOIN items i ON i.id = ii.item_id
 WHERE ii.idea_id = :idea_id AND ii.role = 'seed'
 ORDER BY ii.added_at
 LIMIT 1
"""
)

_SOURCE_CONFIG_SQL = text("SELECT config FROM sources WHERE id = :source_id")

_ANY_ANALYSIS_SQL = text(
    """
SELECT id, revision, created_at FROM analyses
 WHERE idea_id = :idea_id ORDER BY revision DESC LIMIT 1
"""
)

_NEW_SINCE_SQL = text(
    """
SELECT i.external_id, i.title, i.kind
  FROM idea_items ii
  JOIN items i ON i.id = ii.item_id
 WHERE ii.idea_id = :idea_id AND ii.added_at > :since
 ORDER BY ii.added_at
"""
)

_NEXT_REVISION_SQL = text(
    "SELECT coalesce(max(revision), 0) + 1 FROM analyses WHERE idea_id = :idea_id"
)

_INSERT_ANALYSIS_SQL = text(
    """
INSERT INTO analyses (idea_id, revision, lang, model_spec, provider, prompt_version,
                      schema_version, payload, feasibility_score, economics_score,
                      competition_score, verdict, confidence, opportunity_score,
                      tokens_in, tokens_out, cost_usd, latency_ms)
VALUES (:idea_id, :revision, 'it', :model_spec, :provider, :prompt_version,
        :schema_version, CAST(:payload AS jsonb), :feasibility, :economics,
        :competition, :verdict, :confidence, :opportunity,
        :tokens_in, :tokens_out, :cost_usd, :latency_ms)
RETURNING id
"""
)

_SUPERSEDE_SQL = text(
    """
UPDATE analyses SET superseded_by = :new_id
 WHERE idea_id = :idea_id AND superseded_by IS NULL AND id <> :new_id
"""
)

_SET_SUMMARY_SQL = text(
    "UPDATE idea_clusters SET title = :title, canonical_summary = :summary WHERE id = :idea_id"
)

_UPSERT_IDEA_EMBEDDING_SQL = text(
    """
INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec)
VALUES ('idea', :owner_id, :model, :dim, CAST(:vec AS vector))
ON CONFLICT (owner_kind, owner_id, model)
DO UPDATE SET vec = EXCLUDED.vec, dim = EXCLUDED.dim
"""
)

_NULL_RAW_SQL = text("UPDATE items SET raw = NULL WHERE id = ANY(CAST(:ids AS bigint[]))")

_WATCH_ENABLED_SQL = text("SELECT enabled FROM watchlist WHERE idea_id = :idea_id")

_DISABLE_WATCH_SQL = text("UPDATE watchlist SET enabled = false WHERE idea_id = :idea_id")

_SET_STATUS_SQL = text("UPDATE idea_clusters SET status = :status WHERE id = :idea_id")

_IDEA_UPDATE_SQL = text(
    """
INSERT INTO idea_updates (idea_id, item_id, kind, summary)
VALUES (:idea_id, NULL, 'analysis_revision', :summary)
"""
)


def _render_idea(idea) -> str:
    return f"Titolo: {idea['title']}\nSommario canonico: {idea['canonical_summary']}"


def _render_evidence(evidence: list) -> str:
    if not evidence:
        return "(nessuna evidenza)"
    blocks: list[str] = []
    for entry in evidence:
        head = f"[{entry['external_id']}] ({entry['kind']}"
        if entry["score"] is not None:
            head += f", score={entry['score']}"
        if entry["num_comments"] is not None:
            head += f", commenti={entry['num_comments']}"
        head += ")"
        if entry["title"]:
            head += f" {entry['title']}"
        blocks.append(f"{head}\n{(entry['body'] or '').strip()[:600]}")
    return "\n\n".join(blocks)


def _summary_user(idea, evidence: list) -> str:
    top = evidence[:5]
    evidence_text = "\n".join(
        f"[{e['external_id']}] {e['title'] or ''}: {(e['body'] or '').strip()[:400]}" for e in top
    )
    return (
        f"Titolo attuale: {idea['title']}\n"
        f"Sommario attuale: {idea['canonical_summary']}\n"
        f"Evidenze principali:\n{evidence_text}\n\n"
        "Restituisci un titolo breve e un sommario canonico aggiornati, in italiano."
    )


async def _build_delta(session, idea_id: int, previous) -> str:
    if previous is None:
        return "Nessuna revisione precedente: è la prima analisi."
    rows = await session.execute(
        _NEW_SINCE_SQL, {"idea_id": idea_id, "since": previous["created_at"]}
    )
    lines = [f"- [{row['external_id']}] {row['title'] or row['kind']}" for row in rows.mappings()]
    if not lines:
        lines.append("- Nessuna nuova evidenza dal'ultima revisione.")
    return f"Revisione precedente: {previous['revision']}.\nNuove evidenze:\n" + "\n".join(lines)


def _compute_score(payload: AnalysisPayload, seed, evidence_count: int, source_config) -> int:
    saturation = int((source_config or {}).get("engagement_saturation", 5000))
    inputs = ScoreInputs(
        feasibility=payload.feasibility.score,
        economics=payload.economics.score,
        competition=payload.competition.score,
        seed_score=(seed["score"] if seed is not None else None),
        seed_num_comments=(seed["num_comments"] if seed is not None else None),
        seed_is_comment=bool(seed is not None and seed["kind"] == "comment"),
        engagement_saturation=saturation,
        valid_evidence_quotes=evidence_count,
        monetization_model=payload.monetization.model.value,
        confidence=payload.confidence,
    )
    return opportunity_score(inputs)


async def _regenerate_summary(ctx: HandlerContext, idea, evidence: list, settings) -> _Summary:
    result = await ctx.llm.complete_json(
        role=LlmRole.TRIAGE,
        model_spec=settings.triage_model,
        prompt_hash=_SUMMARY_PROMPT_HASH,
        schema_version="v1",
        system=_SUMMARY_SYSTEM,
        user=_summary_user(idea, evidence),
        schema=_SUMMARY_SCHEMA,
        max_tokens=512,
        temperature=0.0,
    )
    return _Summary.model_validate(result.content)


async def handle_analyze(job, ctx: HandlerContext) -> None:
    """Produce (o aggiorna) la valutazione strutturata di un'idea."""
    settings = ctx.settings
    idea_id = int(job.payload["idea_id"])

    async with ctx.db() as session:
        idea = (await session.execute(_IDEA_SQL, {"idea_id": idea_id})).mappings().one_or_none()
        if idea is None:
            await finish(session, job.id)
            await session.commit()
            return

        params = {"idea_id": idea_id, "limit": settings.analysis_max_evidence}
        params.update(_DEMAND_PARAMS)
        evidence = list((await session.execute(_EVIDENCE_SQL, params)).mappings())
        seed = (await session.execute(_SEED_SQL, {"idea_id": idea_id})).mappings().one_or_none()
        previous = (
            (await session.execute(_ANY_ANALYSIS_SQL, {"idea_id": idea_id}))
            .mappings()
            .one_or_none()
        )

        source_config = None
        if seed is not None:
            source_config = await session.scalar(
                _SOURCE_CONFIG_SQL, {"source_id": int(seed["source_id"])}
            )

        delta = await _build_delta(session, idea_id, previous)
        prompt = load_prompt("analysis", "v1")
        result = await ctx.llm.analyst().complete_json(
            model=settings.analyst_model,
            system=prompt.system,
            user=prompt.render_user(
                idea=_render_idea(idea),
                evidence=_render_evidence(evidence),
                delta=delta,
            ),
            schema=AnalysisPayload.model_json_schema(),
            max_tokens=prompt.max_tokens,
            temperature=prompt.temperature,
        )
        payload = AnalysisPayload.model_validate(result.content)

        allowed = {entry["external_id"] for entry in evidence}
        quotes = [
            EvidenceQuote(
                item_external_id=quote.item_external_id,
                quote=quote.quote[: settings.quote_max_chars],
            )
            for quote in payload.evidence_quotes
            if quote.item_external_id in allowed
        ]
        payload = payload.model_copy(update={"evidence_quotes": quotes})

        score = _compute_score(payload, seed, len(quotes), source_config)

        revision = int(await session.scalar(_NEXT_REVISION_SQL, {"idea_id": idea_id}))
        new_id = int(
            await session.scalar(
                _INSERT_ANALYSIS_SQL,
                {
                    "idea_id": idea_id,
                    "revision": revision,
                    "model_spec": settings.analyst_model,
                    "provider": result.provider,
                    "prompt_version": prompt.version,
                    "schema_version": prompt.schema_version,
                    "payload": payload.model_dump_json(),
                    "feasibility": payload.feasibility.score,
                    "economics": payload.economics.score,
                    "competition": payload.competition.score,
                    "verdict": payload.verdict.value,
                    "confidence": payload.confidence,
                    "opportunity": score,
                    "tokens_in": result.tokens_in,
                    "tokens_out": result.tokens_out,
                    "cost_usd": result.cost_usd,
                    "latency_ms": result.latency_ms,
                },
            )
        )
        await session.execute(_SUPERSEDE_SQL, {"new_id": new_id, "idea_id": idea_id})

        summary = await _regenerate_summary(ctx, idea, evidence, settings)
        await session.execute(
            _SET_SUMMARY_SQL,
            {"idea_id": idea_id, "title": summary.title, "summary": summary.canonical_summary},
        )
        vectors = await ctx.llm.embedder().embed(
            model=settings.embed_model,
            texts=[f"{summary.title}\n{summary.canonical_summary}"],
        )
        vector = vectors[0]
        await session.execute(
            _UPSERT_IDEA_EMBEDDING_SQL,
            {
                "owner_id": idea_id,
                "model": settings.embed_model,
                "dim": len(vector),
                "vec": vec_literal(vector),
            },
        )

        if evidence:
            await session.execute(_NULL_RAW_SQL, {"ids": [int(entry["id"]) for entry in evidence]})

        if payload.verdict is Verdict.REJECT:
            await session.execute(_DISABLE_WATCH_SQL, {"idea_id": idea_id})
            status = "rejected"
        else:
            watching = await session.scalar(_WATCH_ENABLED_SQL, {"idea_id": idea_id})
            status = "watching" if watching else "analyzed"
        await session.execute(_SET_STATUS_SQL, {"status": status, "idea_id": idea_id})

        await session.execute(
            _IDEA_UPDATE_SQL,
            {
                "idea_id": idea_id,
                "summary": (
                    f"Nuova revisione di analisi (rev {revision}, verdict {payload.verdict.value})"
                ),
            },
        )
        await ctx.queue.enqueue("pipeline.score", {"idea_id": idea_id})

        await finish(session, job.id)
        await session.commit()
        log.info(
            "analisi conclusa",
            job_id=job.id,
            idea_id=idea_id,
            revision=revision,
            verdict=payload.verdict.value,
        )


__all__ = ["handle_analyze"]
