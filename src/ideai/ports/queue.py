"""Porta coda (§3.3). Cinque metodi verbatim più ``defer`` (divergenza 3 del piano)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ideai.domain import Job


class Queue(Protocol):
    """Contratto della coda su PostgreSQL: claim atomico con ``SKIP LOCKED``."""

    async def enqueue(
        self,
        topic: str,
        payload: dict,
        *,
        dedup_key: str | None = None,
        priority: int = 100,
        run_after: datetime | None = None,
    ) -> int: ...

    async def claim(self, topics: list[str], worker_id: str, lease_s: int) -> Job | None: ...

    async def heartbeat(self, job_id: int) -> None: ...

    async def complete(self, job_id: int) -> None: ...

    async def fail(self, job_id: int, error: str, *, retryable: bool = True) -> None: ...

    async def defer(self, job_id: int, run_after: datetime) -> None:
        """Riporta il job ``pending`` con il ``run_after`` indicato, senza toccare ``attempts``.

        Serve al rinvio per budget esaurito (§6.8): ``fail`` non può esprimerlo perché
        incrementerebbe o consumerebbe un tentativo.
        """
        ...
