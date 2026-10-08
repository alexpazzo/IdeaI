"""Contesto condiviso degli handler della pipeline (§6.3, §6.5).

``HandlerContext`` è costruito dal worker (e dal doctor) con esattamente i campi
``settings``, ``db``, ``queue``, ``llm``, ``rate``, ``adapters``, ``now``: è il
contratto vincolante fra il runtime e gli handler.

Ogni handler applica la regola di idempotenza del piano: **tutti** gli effetti su
DB di un job avvengono in una sola transazione che termina con :func:`finish`.
``Queue.complete()`` è quindi di norma un no-op e l'unico modo in cui un job torna
``pending`` è il reaper (lease scaduta, transazione rollbackata → nessun effetto).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ideai.domain import Job

#: Firma di un handler: ``await handler(job, ctx)``.
Handler = Callable[[Job, "HandlerContext"], Awaitable[None]]

_FINISH_SQL = text(
    """
UPDATE jobs SET state = 'done', finished_at = now(), locked_by = NULL
 WHERE id = :job_id
"""
)


@dataclass
class HandlerContext:
    """Dipendenze condivise degli handler, iniettate dal worker."""

    settings: Any
    db: Any
    queue: Any
    llm: Any
    rate: Any
    adapters: dict[str, Any]
    now: Callable[[], datetime]


async def finish(session: AsyncSession, job_id: int) -> None:
    """Marca il job ``done``; va eseguita come **ultima** istruzione della transazione."""
    await session.execute(_FINISH_SQL, {"job_id": job_id})


__all__ = ["Handler", "HandlerContext", "finish"]
