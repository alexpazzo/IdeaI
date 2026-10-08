"""Scheduler: emette i job periodici, singolo per deployment (§6.6).

Tiene ``pg_advisory_lock(hashtext('ideai.scheduler'))`` su una connessione
dedicata fuori dal pool (lock di sessione, non transazionale): una seconda
replica resta in attesa del lock e non emette nulla. È l'unico proprietario di
``source_targets.next_poll_at``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ideai.logging import get_logger
from ideai.runtime.reaper import reap_once

log = get_logger(__name__)

_DUE_TARGETS_SQL = text(
    """
UPDATE source_targets SET next_poll_at = now() + make_interval(secs => poll_interval_s)
 WHERE enabled AND next_poll_at <= now()
 RETURNING id, next_poll_at
"""
)

_DUE_WATCHES_SQL = text(
    """
SELECT idea_id FROM watchlist
 WHERE enabled
   AND COALESCE(last_checked_at, to_timestamp(0)) + make_interval(secs => interval_s) <= now()
"""
)

_LAST_RETENTION_SQL = text(
    """
SELECT max(finished_at) FROM jobs
 WHERE topic = 'maintenance.retention' AND state = 'done'
"""
)


def _epoch_minutes(when: datetime) -> int:
    """Minuto epoch: parte del ``dedup_key`` deterministico (§6.6)."""
    return int(when.timestamp() // 60)


async def _tick(settings, session_factory: async_sessionmaker[AsyncSession], queue) -> None:
    """Un giro dello scheduler; ogni tick ricalcola scadenze e accoda i job."""
    async with session_factory() as session:
        targets = (await session.execute(_DUE_TARGETS_SQL)).mappings().all()
        watches = (await session.execute(_DUE_WATCHES_SQL)).mappings().all()
        last_retention = (await session.execute(_LAST_RETENTION_SQL)).scalar_one()
        await session.commit()

    now = datetime.now(UTC)

    for target in targets:
        await queue.enqueue(
            "pipeline.scrape",
            {"target_id": target["id"]},
            dedup_key=f"scrape:{target['id']}:{_epoch_minutes(target['next_poll_at'])}",
        )
    for watch in watches:
        await queue.enqueue(
            "pipeline.watch",
            {"idea_id": watch["idea_id"]},
            dedup_key=f"watch:{watch['idea_id']}:{_epoch_minutes(now)}",
        )
    if last_retention is None or last_retention < now - timedelta(hours=24):
        await queue.enqueue("maintenance.retention", {})


async def run_scheduler(settings, session_factory: async_sessionmaker[AsyncSession], queue) -> None:
    """Loop dello scheduler: lock di sessione, tick periodico, reaper a fine giro."""
    engine = session_factory.kw.get("bind") if hasattr(session_factory, "kw") else None
    if engine is None:
        raise RuntimeError("run_scheduler richiede una session_factory con engine (bind)")

    lock_conn = await engine.connect()
    try:
        await lock_conn.execute(text("SELECT pg_advisory_lock(hashtext('ideai.scheduler'))"))
        await lock_conn.commit()
        log.info("scheduler: lock acquisito")

        while True:
            try:
                await _tick(settings, session_factory, queue)
                await reap_once(session_factory, settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - il tick non deve fermare il loop
                log.error("scheduler: tick fallito", error=repr(exc))
            await asyncio.sleep(settings.scheduler_tick_s)
    finally:
        await lock_conn.close()


__all__ = ["run_scheduler"]
