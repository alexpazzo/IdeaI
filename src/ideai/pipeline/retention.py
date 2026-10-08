"""Handler ``maintenance.retention``: potatura giornaliera (§6.10).

In una sola transazione, nell'ordine: item rifiutati vecchi, azzeramento di ``raw``
per gli item già analizzati, cache LLM scaduta e job conclusi vecchi.
"""

from __future__ import annotations

from sqlalchemy import text

from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext, finish

log = get_logger(__name__)

_DELETE_REJECTED_SQL = text(
    """
DELETE FROM items
 WHERE state = 'rejected'
   AND fetched_at < now() - make_interval(days => :retention_days)
"""
)

_NULL_RAW_SQL = text(
    """
UPDATE items SET raw = NULL
 WHERE raw IS NOT NULL
   AND state IN ('candidate', 'analyzed')
   AND idea_id IN (SELECT DISTINCT idea_id FROM analyses)
"""
)

_DELETE_CACHE_SQL = text(
    """
DELETE FROM llm_cache
 WHERE created_at < now() - make_interval(days => :cache_days)
"""
)

_DELETE_JOBS_SQL = text(
    """
DELETE FROM jobs
 WHERE state = 'done'
   AND finished_at < now() - make_interval(days => :job_days)
"""
)


async def handle_retention(job, ctx: HandlerContext) -> None:
    """Esegue la retention giornaliera e marca il job ``done``."""
    settings = ctx.settings

    async with ctx.db() as session:
        rejected = (
            await session.execute(_DELETE_REJECTED_SQL, {"retention_days": settings.retention_days})
        ).rowcount
        zeroed = (await session.execute(_NULL_RAW_SQL)).rowcount
        cache = (
            await session.execute(
                _DELETE_CACHE_SQL, {"cache_days": settings.llm_cache_max_age_days}
            )
        ).rowcount
        jobs = (
            await session.execute(_DELETE_JOBS_SQL, {"job_days": settings.job_retention_days})
        ).rowcount

        await finish(session, job.id)
        await session.commit()
        log.info(
            "retention conclusa",
            job_id=job.id,
            item_eliminati=rejected,
            raw_azzerati=zeroed,
            cache_eliminate=cache,
            job_eliminati=jobs,
        )


__all__ = ["handle_retention"]
