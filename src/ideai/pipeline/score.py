"""Handler ``pipeline.score``: opportunità deterministica e watch automatico (§8.4, §8.5).

Il punteggio è calcolato dall'ultima analisi non superata e dal seed; il watch
automatico nasce solo se l'idea supera ``IDEAI_WATCH_THRESHOLD`` e la sorgente del
seed espone i commenti. Una riga ``watchlist`` disabilitata **non** viene mai
riabilitata da qui (§8.5): l'utente può solo riattivarla esplicitamente.
"""

from __future__ import annotations

from sqlalchemy import text

from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext, finish
from ideai.scoring import ScoreInputs, opportunity_score

log = get_logger(__name__)

_IDEA_SQL = text("SELECT id, status, opportunity_score FROM idea_clusters WHERE id = :idea_id")

_SEED_SQL = text(
    """
SELECT i.id, i.kind, i.score, i.num_comments, i.source_id, s.kind AS source_kind
  FROM idea_items ii
  JOIN items i ON i.id = ii.item_id
  JOIN sources s ON s.id = i.source_id
 WHERE ii.idea_id = :idea_id AND ii.role = 'seed'
 ORDER BY ii.added_at
 LIMIT 1
"""
)

_SOURCE_CONFIG_SQL = text("SELECT config FROM sources WHERE id = :source_id")

_LATEST_ANALYSIS_SQL = text(
    """
SELECT feasibility_score, economics_score, competition_score, verdict, confidence, payload
  FROM analyses
 WHERE idea_id = :idea_id AND superseded_by IS NULL
 ORDER BY revision DESC
 LIMIT 1
"""
)

_UPDATE_SCORE_SQL = text(
    """
UPDATE idea_clusters
   SET opportunity_score = :score,
       score_version = 'v1',
       item_count = (SELECT count(*) FROM idea_items WHERE idea_id = :idea_id),
       source_kinds = COALESCE((SELECT array_agg(DISTINCT s.kind)
                                  FROM idea_items ii
                                  JOIN items i ON i.id = ii.item_id
                                  JOIN sources s ON s.id = i.source_id
                                 WHERE ii.idea_id = :idea_id), '{}'),
       last_activity_at = now()
 WHERE id = :idea_id
"""
)

_WATCH_ROW_SQL = text("SELECT enabled FROM watchlist WHERE idea_id = :idea_id")

_INSERT_WATCH_SQL = text(
    """
INSERT INTO watchlist (idea_id, enabled, interval_s, mode, added_by)
VALUES (:idea_id, true, 3600, 'thread_full', 'auto')
ON CONFLICT (idea_id) DO NOTHING
"""
)

_SET_STATUS_SQL = text("UPDATE idea_clusters SET status = :status WHERE id = :idea_id")


async def handle_score(job, ctx: HandlerContext) -> None:
    """Calcola ``opportunity_score`` e decide il watch automatico."""
    settings = ctx.settings
    idea_id = int(job.payload["idea_id"])

    async with ctx.db() as session:
        idea = (await session.execute(_IDEA_SQL, {"idea_id": idea_id})).mappings().one_or_none()
        if idea is None:
            await finish(session, job.id)
            await session.commit()
            return

        analysis = (
            (await session.execute(_LATEST_ANALYSIS_SQL, {"idea_id": idea_id}))
            .mappings()
            .one_or_none()
        )
        if analysis is None:
            await finish(session, job.id)
            await session.commit()
            return

        seed = (await session.execute(_SEED_SQL, {"idea_id": idea_id})).mappings().one_or_none()
        source_config = None
        if seed is not None:
            source_config = await session.scalar(
                _SOURCE_CONFIG_SQL, {"source_id": int(seed["source_id"])}
            )

        payload = analysis["payload"] or {}
        quotes = payload.get("evidence_quotes") or []
        monetization = (payload.get("monetization") or {}).get("model", "unknown")
        inputs = ScoreInputs(
            feasibility=analysis["feasibility_score"] or 1,
            economics=analysis["economics_score"] or 1,
            competition=analysis["competition_score"] or 1,
            seed_score=(seed["score"] if seed is not None else None),
            seed_num_comments=(seed["num_comments"] if seed is not None else None),
            seed_is_comment=bool(seed is not None and seed["kind"] == "comment"),
            engagement_saturation=int((source_config or {}).get("engagement_saturation", 5000)),
            valid_evidence_quotes=len(quotes),
            monetization_model=monetization,
            confidence=float(analysis["confidence"] or 0.0),
        )
        score = opportunity_score(inputs)
        await session.execute(_UPDATE_SCORE_SQL, {"score": score, "idea_id": idea_id})

        has_comments = False
        if seed is not None:
            adapter = ctx.adapters.get(seed["source_kind"])
            if adapter is not None:
                has_comments = adapter.capabilities().has_comments

        watch = await session.scalar(_WATCH_ROW_SQL, {"idea_id": idea_id})
        status = idea["status"]
        if watch is None:
            if score >= settings.watch_threshold and has_comments:
                await session.execute(_INSERT_WATCH_SQL, {"idea_id": idea_id})
                status = "watching"
            elif status == "watching":
                status = "analyzed"
        elif watch:
            status = "watching"
        # riga presente ma disabilitata: nessuna riabilitazione (§8.5), stato invariato

        if status != idea["status"]:
            await session.execute(_SET_STATUS_SQL, {"status": status, "idea_id": idea_id})

        await finish(session, job.id)
        await session.commit()
        log.info("score concluso", job_id=job.id, idea_id=idea_id, score=score, status=status)


__all__ = ["handle_score"]
