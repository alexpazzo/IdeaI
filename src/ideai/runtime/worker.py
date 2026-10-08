"""Worker: N task asyncio che reclamano e eseguono job (§6.5).

Costruisce il contesto condiviso (coda, gateway LLM, rate limiter, adapter
sorgente) e invoca gli handler della pipeline dal registry. Gli handler
marcano ``done`` nella propria transazione, quindi ``complete`` è di norma un
no-op; l'unico modo in cui un job torna ``pending`` è il reaper.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from datetime import UTC, datetime

from ideai import metrics
from ideai.adapters.llm import build_transports
from ideai.adapters.llm.gateway import LlmGateway
from ideai.adapters.queue_pg import PgQueue
from ideai.adapters.sources.registry import build_adapters
from ideai.domain import BudgetExhausted, NonRetryableError
from ideai.logging import get_logger
from ideai.pipeline.context import HandlerContext
from ideai.pipeline.registry import handler_for
from ideai.runtime.ratelimit import RateLimiter

log = get_logger(__name__)


async def _heartbeat_loop(queue: PgQueue, job_id: int, interval_s: int) -> None:
    """Aggiorna ``heartbeat_at`` del job finché il task non viene cancellato."""
    while True:
        await queue.heartbeat(job_id)
        await asyncio.sleep(interval_s)


async def _worker_loop(
    settings,
    queue: PgQueue,
    ctx: HandlerContext,
    topics: list[str],
    worker_id: str,
) -> None:
    """Un ciclo di claim/esecuzione; gira per tutta la vita del processo."""
    while True:
        job = await queue.claim(topics, worker_id, settings.job_lease_s)
        if job is None:
            await asyncio.sleep(1.0)
            continue

        heartbeat = asyncio.create_task(_heartbeat_loop(queue, job.id, settings.job_heartbeat_s))
        try:
            await handler_for(job.topic)(job, ctx)
            metrics.jobs_total.labels(state="done", topic=job.topic).inc()
            await queue.complete(job.id)
            log.info("job concluso", job_id=job.id, topic=job.topic, worker_id=worker_id)
        except BudgetExhausted as exc:
            await queue.defer(job.id, PgQueue.deferral_run_after())
            log.warning(
                "job rinviato per budget esaurito",
                job_id=job.id,
                topic=job.topic,
                error=repr(exc),
            )
        except NonRetryableError as exc:
            await queue.fail(job.id, repr(exc), retryable=False)
            metrics.jobs_total.labels(state="dead", topic=job.topic).inc()
            log.error("job non ritentabile", job_id=job.id, topic=job.topic, error=repr(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - qualunque errore va registrato e ritentato
            await queue.fail(job.id, repr(exc))
            metrics.jobs_total.labels(state="pending", topic=job.topic).inc()
            log.error("job fallito", job_id=job.id, topic=job.topic, error=repr(exc))
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat


async def run_worker(settings, session_factory, topics: list[str], concurrency: int) -> None:
    """Avvia ``concurrency`` task worker sui topic indicati."""
    queue = PgQueue(session_factory, settings)
    limiter = RateLimiter(session_factory)
    transports = build_transports(settings)
    gateway = LlmGateway(settings, session_factory, transports, limiter)
    adapters = build_adapters(settings)

    ctx = HandlerContext(
        settings=settings,
        db=session_factory,
        queue=queue,
        llm=gateway,
        rate=limiter,
        adapters=adapters,
        now=lambda: datetime.now(UTC),
    )

    host = socket.gethostname()
    pid = os.getpid()
    tasks = [
        asyncio.create_task(_worker_loop(settings, queue, ctx, topics, f"{host}-{pid}-{i}"))
        for i in range(concurrency)
    ]
    log.info("worker avviati", topics=topics, concurrency=concurrency)
    await asyncio.gather(*tasks)


__all__ = ["run_worker"]
