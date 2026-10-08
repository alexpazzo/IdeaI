"""Reaper: reclama i job con lease scaduta (§6.5).

Rete di sicurezza dell'at-least-once: un worker ucciso lascia il job ``running``
con ``heartbeat_at`` fermo. Il reaper lo riporta ``pending`` (o ``dead`` se ha
esaurito i tentativi) così il lavoro non resta bloccato.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ideai.logging import get_logger

log = get_logger(__name__)

_REAP_SQL = text(
    """
UPDATE jobs
   SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
       finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
       locked_by = NULL,
       last_error = COALESCE(last_error, 'lease expired')
 WHERE state = 'running' AND heartbeat_at < now() - make_interval(secs => :lease)
 RETURNING id, state
"""
)


async def reap_once(
    session_factory: async_sessionmaker[AsyncSession], settings
) -> list[tuple[int, str]]:
    """Esegue un giro di bonifica e ritorna le righe ``(job_id, nuovo_stato)``."""
    async with session_factory() as session:
        rows = (await session.execute(_REAP_SQL, {"lease": settings.job_lease_s})).mappings().all()
        await session.commit()

    reaped = [(row["id"], row["state"]) for row in rows]
    for job_id, state in reaped:
        log.warning("reaper: job recuperato", job_id=job_id, state=state)
    return reaped


async def run_reaper(session_factory: async_sessionmaker[AsyncSession], settings) -> None:
    """Giro di bonifica ogni 60 secondi, per tutta la vita del processo."""
    while True:
        try:
            await reap_once(session_factory, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - il reaper non deve mai morire
            log.error("reaper: giro fallito", error=repr(exc))
        await asyncio.sleep(60.0)


__all__ = ["reap_once", "run_reaper"]
