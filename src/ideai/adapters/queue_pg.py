"""Adapter PostgreSQL della porta coda (§6.1–6.5).

La coda è la tabella ``jobs``; il claim è un ``UPDATE ... RETURNING *`` con
sotto-query ``FOR UPDATE SKIP LOCKED``. Le scritture sono guardate dallo stato
atteso, così ``complete``/``fail``/``defer`` sono no-op se il job è già passato
oltre (per esempio perché l'handler ha marcato ``done`` nella propria
transazione).
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ideai.domain import Job, JobState

# Claim verbatim di §6.2.
_CLAIM_SQL = text(
    """
UPDATE jobs
   SET state = 'running',
       locked_by = :worker_id,
       locked_at = now(),
       heartbeat_at = now(),
       attempts = attempts + 1
 WHERE id = (
       SELECT id FROM jobs
        WHERE state = 'pending'
          AND topic = ANY(CAST(:topics AS text[]))
          AND run_after <= now()
        ORDER BY priority, run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1)
 RETURNING *
"""
)

_INSERT_SQL = text(
    """
INSERT INTO jobs (topic, dedup_key, payload, priority, run_after, max_attempts)
VALUES (:topic, :dedup_key, CAST(:payload AS jsonb), :priority, :run_after, :max_attempts)
ON CONFLICT DO NOTHING
RETURNING id
"""
)

_HEARTBEAT_SQL = text(
    """
UPDATE jobs SET heartbeat_at = now()
 WHERE id = :job_id AND state = 'running'
"""
)

_COMPLETE_SQL = text(
    """
UPDATE jobs SET state = 'done', finished_at = now(), locked_by = NULL
 WHERE id = :job_id AND state = 'running'
"""
)

_FAIL_DEAD_SQL = text(
    """
UPDATE jobs SET state = 'dead', finished_at = now(), last_error = :error, locked_by = NULL
 WHERE id = :job_id AND state = 'running'
"""
)

# Il jitter è calcolato in Python e passato come fattore moltiplicativo.
_FAIL_RETRY_SQL = text(
    """
UPDATE jobs SET
       state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
       finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
       locked_by = NULL,
       locked_at = NULL,
       heartbeat_at = NULL,
       last_error = :error,
       run_after = CASE
           WHEN attempts >= max_attempts THEN run_after
           ELSE now() + make_interval(secs => LEAST(3600, power(2, attempts) * 15) * :jitter)
       END
 WHERE id = :job_id AND state = 'running'
"""
)

_DEFER_SQL = text(
    """
UPDATE jobs SET state = 'pending', run_after = :run_after,
       locked_by = NULL, locked_at = NULL, heartbeat_at = NULL
 WHERE id = :job_id AND state = 'running'
"""
)


def _row_to_job(row: Any) -> Job:
    """Mappa una riga ``RETURNING *`` delle ``jobs`` nella dataclass ``Job``."""
    return Job(
        id=row["id"],
        topic=row["topic"],
        payload=row["payload"],
        state=row["state"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        locked_by=row["locked_by"],
        run_after=row["run_after"],
    )


class PgQueue:
    """Coda su PostgreSQL: at-least-once con dedup parziale su ``dedup_key``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], settings: Any = None):
        self._sf = session_factory
        # Accetta sia ``Settings`` sia direttamente il numero massimo di tentativi.
        if settings is None:
            self._max_attempts = 5
        else:
            self._max_attempts = getattr(settings, "max_attempts", settings)

    async def _try_insert(
        self,
        session: AsyncSession,
        *,
        topic: str,
        dedup_key: str | None,
        payload: dict,
        priority: int,
        run_after: datetime,
    ) -> int | None:
        result = await session.execute(
            _INSERT_SQL,
            {
                "topic": topic,
                "dedup_key": dedup_key,
                "payload": json.dumps(payload),
                "priority": priority,
                "run_after": run_after,
                "max_attempts": self._max_attempts,
            },
        )
        return result.scalar_one_or_none()

    async def enqueue(
        self,
        topic: str,
        payload: dict,
        *,
        dedup_key: str | None = None,
        priority: int = 100,
        run_after: datetime | None = None,
    ) -> int:
        """Inserisce un job; con ``dedup_key`` è idempotente finché il job è pending/running."""
        run_after = run_after or datetime.now(UTC)
        async with self._sf() as session:
            new_id = await self._try_insert(
                session,
                topic=topic,
                dedup_key=dedup_key,
                payload=payload,
                priority=priority,
                run_after=run_after,
            )
            if new_id is None:
                if dedup_key is None:
                    # Nessun vincolo unico può aver conflitto: l'insert non può fallire.
                    await session.commit()
                    raise RuntimeError("insert del job senza dedup_key conflittuale")
                existing = (
                    await session.execute(
                        text(
                            """
SELECT id FROM jobs
 WHERE dedup_key = :dedup_key AND state IN ('pending','running')
 ORDER BY id DESC LIMIT 1
"""
                        ),
                        {"dedup_key": dedup_key},
                    )
                ).scalar_one_or_none()
                if existing is None:
                    # Il job precedente si è concluso nel frattempo: un solo nuovo tentativo.
                    new_id = await self._try_insert(
                        session,
                        topic=topic,
                        dedup_key=dedup_key,
                        payload=payload,
                        priority=priority,
                        run_after=run_after,
                    )
                    if new_id is None:
                        new_id = (
                            await session.execute(
                                text(
                                    """
SELECT id FROM jobs WHERE dedup_key = :dedup_key
 ORDER BY id DESC LIMIT 1
"""
                                ),
                                {"dedup_key": dedup_key},
                            )
                        ).scalar_one_or_none()
                else:
                    new_id = existing
            await session.commit()
            assert new_id is not None  # l'insert o il fallback hanno sempre una riga
            return int(new_id)

    async def claim(self, topics: list[str], worker_id: str, lease_s: int) -> Job | None:
        """Reclama il job pending più urgente fra i topic indicati (§6.2)."""
        async with self._sf() as session:
            row = (
                (
                    await session.execute(
                        _CLAIM_SQL, {"worker_id": worker_id, "topics": list(topics)}
                    )
                )
                .mappings()
                .first()
            )
            await session.commit()
            return _row_to_job(row) if row is not None else None

    async def heartbeat(self, job_id: int) -> None:
        async with self._sf() as session:
            await session.execute(_HEARTBEAT_SQL, {"job_id": job_id})
            await session.commit()

    async def complete(self, job_id: int) -> None:
        async with self._sf() as session:
            await session.execute(_COMPLETE_SQL, {"job_id": job_id})
            await session.commit()

    async def fail(self, job_id: int, error: str, *, retryable: bool = True) -> None:
        """Backoff esponenziale con jitter, oppure ``dead`` immediato/non ritentabile."""
        if not retryable:
            async with self._sf() as session:
                await session.execute(_FAIL_DEAD_SQL, {"job_id": job_id, "error": error})
                await session.commit()
            return
        jitter = 1.0 + random.uniform(-0.2, 0.2)  # noqa: S311 - jitter non crittografico
        async with self._sf() as session:
            await session.execute(
                _FAIL_RETRY_SQL, {"job_id": job_id, "error": error, "jitter": jitter}
            )
            await session.commit()

    async def defer(self, job_id: int, run_after: datetime) -> None:
        """Rinvia il job senza toccare ``attempts`` (budget esaurito, §6.8)."""
        async with self._sf() as session:
            await session.execute(_DEFER_SQL, {"job_id": job_id, "run_after": run_after})
            await session.commit()

    @staticmethod
    def deferral_run_after() -> datetime:
        """Mezzanotte UTC del giorno successivo, coerente con ``v_daily_llm_cost`` (§6.8)."""
        now = datetime.now(UTC)
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


__all__ = ["PgQueue", "JobState"]
