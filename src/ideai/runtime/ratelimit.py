"""Rate limiting per bucket su PostgreSQL (§6.7).

Il lock consultivo è transazionale (``pg_advisory_xact_lock``): serializza i
worker che parlano allo stesso bucket senza bloccare quelli su bucket diversi e
si rilascia da solo a fine transazione. L'attesa avviene **fuori** dalla
transazione, con ``asyncio.sleep`` fino alla fine della finestra corrente: mai
dormire tenendo il lock.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

_ACQUIRE_SQL = text(
    """
INSERT INTO rate_limits (bucket, window_start, used)
VALUES (:bucket, now(), 1)
ON CONFLICT (bucket) DO UPDATE SET
    used = CASE
        WHEN rate_limits.window_start <= now() - make_interval(secs => :window_s) THEN 1
        ELSE rate_limits.used + 1
    END,
    window_start = CASE
        WHEN rate_limits.window_start <= now() - make_interval(secs => :window_s) THEN now()
        ELSE rate_limits.window_start
    END
RETURNING window_start, used
"""
)

_RECORD_CALL_SQL = text(
    """
INSERT INTO source_calls_daily (day, source_id, calls, cost_usd)
VALUES ((now() AT TIME ZONE 'UTC')::date, :source_id, 1, :cost_usd)
ON CONFLICT (day, source_id) DO UPDATE SET
    calls = source_calls_daily.calls + 1,
    cost_usd = source_calls_daily.cost_usd + EXCLUDED.cost_usd
"""
)


async def record_source_call(
    conn: AsyncConnection, source_id: int, cost_per_call_usd: float
) -> None:
    """Contabilizza una chiamata esterna nel giorno UTC corrente, senza committare.

    Va eseguita nello **stesso passo** del rate limit, prima della richiesta HTTP.
    """
    await conn.execute(_RECORD_CALL_SQL, {"source_id": source_id, "cost_usd": cost_per_call_usd})


class RateLimiter:
    """Semaforo distribuito per bucket basato sulla tabella ``rate_limits``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._sf = session_factory

    async def acquire(self, bucket: str, requests_per_minute: int, window_s: int) -> None:
        """Attende finché uno slot del bucket non è disponibile, poi lo consuma."""
        while True:
            async with self._sf() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:bucket))"),
                    {"bucket": bucket},
                )
                row = (
                    (await session.execute(_ACQUIRE_SQL, {"bucket": bucket, "window_s": window_s}))
                    .mappings()
                    .one()
                )
                await session.commit()

            if row["used"] <= requests_per_minute:
                return

            deadline = row["window_start"] + timedelta(seconds=window_s)
            sleep_s = (deadline - datetime.now(UTC)).total_seconds()
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

    async def record_source_call(
        self, conn: AsyncConnection, source_id: int, cost_per_call_usd: float
    ) -> None:
        """Variante metodo di :func:`record_source_call` (stessa semantica)."""
        await record_source_call(conn, source_id, cost_per_call_usd)


__all__ = ["RateLimiter", "record_source_call"]
