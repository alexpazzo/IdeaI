"""Test di coda, reaper e rate limiting su Postgres reale (§11.4c)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ideai.adapters.queue_pg import PgQueue
from ideai.runtime.ratelimit import RateLimiter
from ideai.runtime.reaper import reap_once


async def _job_row(session_factory, job_id: int) -> dict:
    async with session_factory() as session:
        row = (
            (await session.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}))
            .mappings()
            .one()
        )
        await session.commit()
        return dict(row)


async def _set_run_after_now(session_factory, job_id: int) -> None:
    async with session_factory() as session:
        await session.execute(
            text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id}
        )
        await session.commit()


async def test_claim_orders_by_priority_and_run_after(session_factory, settings):
    queue = PgQueue(session_factory, settings)
    now = datetime.now(UTC)

    late = await queue.enqueue(
        "pipeline.score", {"n": "late"}, priority=100, run_after=now + timedelta(hours=1)
    )
    low_priority = await queue.enqueue(
        "pipeline.score", {"n": "low"}, priority=200, run_after=now - timedelta(seconds=10)
    )
    high_priority = await queue.enqueue(
        "pipeline.score", {"n": "high"}, priority=100, run_after=now - timedelta(seconds=10)
    )

    first = await queue.claim(["pipeline.score"], "w1", 120)
    second = await queue.claim(["pipeline.score"], "w1", 120)
    third = await queue.claim(["pipeline.score"], "w1", 120)

    assert first is not None and first.id == high_priority
    assert second is not None and second.id == low_priority
    assert third is None  # il job con run_after futuro non è ancora eleggibile
    assert late not in (first.id, second.id)


async def test_dedup_key_blocks_second_pending_job(session_factory, settings):
    queue = PgQueue(session_factory, settings)

    first = await queue.enqueue("pipeline.scrape", {"target_id": 1}, dedup_key="scrape:1:10")
    second = await queue.enqueue("pipeline.scrape", {"target_id": 1}, dedup_key="scrape:1:10")

    assert second == first
    async with session_factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE dedup_key = :k"),
                {"k": "scrape:1:10"},
            )
        ).scalar_one()
    assert count == 1


async def test_complete_then_same_dedup_key_enqueues_new_job(session_factory, settings):
    queue = PgQueue(session_factory, settings)

    first = await queue.enqueue("pipeline.scrape", {"target_id": 1}, dedup_key="scrape:1:10")
    job = await queue.claim(["pipeline.scrape"], "w1", 120)
    assert job is not None and job.id == first
    await queue.complete(first)

    second = await queue.enqueue("pipeline.scrape", {"target_id": 1}, dedup_key="scrape:1:10")
    assert second != first


async def test_fail_retryable_backoff_then_dead_after_max_attempts(session_factory, settings):
    queue = PgQueue(session_factory, settings)
    job_id = await queue.enqueue("pipeline.score", {})

    for expected_attempts in range(1, settings.max_attempts + 1):
        before = datetime.now(UTC)
        job = await queue.claim(["pipeline.score"], "w1", 120)
        assert job is not None and job.id == job_id
        assert job.attempts == expected_attempts
        await queue.fail(job.id, "boom")

        row = await _job_row(session_factory, job_id)
        assert row["attempts"] == expected_attempts
        if expected_attempts < settings.max_attempts:
            assert row["state"] == "pending"
            assert row["run_after"] > before
            await _set_run_after_now(session_factory, job_id)
        else:
            assert row["state"] == "dead"
            assert row["last_error"] == "boom"
            assert row["finished_at"] is not None


async def test_fail_non_retryable_is_dead_immediately(session_factory, settings):
    queue = PgQueue(session_factory, settings)
    job_id = await queue.enqueue("pipeline.score", {})
    job = await queue.claim(["pipeline.score"], "w1", 120)
    assert job is not None

    await queue.fail(job.id, "payload malformato", retryable=False)

    row = await _job_row(session_factory, job_id)
    assert row["state"] == "dead"
    assert row["last_error"] == "payload malformato"
    assert row["locked_by"] is None


async def test_defer_keeps_attempts(session_factory, settings):
    queue = PgQueue(session_factory, settings)
    job_id = await queue.enqueue("pipeline.analyze", {"idea_id": 1})
    job = await queue.claim(["pipeline.analyze"], "w1", 120)
    assert job is not None and job.attempts == 1

    run_after = PgQueue.deferral_run_after()
    await queue.defer(job.id, run_after)

    row = await _job_row(session_factory, job_id)
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["locked_by"] is None
    assert abs((row["run_after"] - run_after).total_seconds()) < 1
    assert run_after.hour == 0 and run_after.minute == 0 and run_after.second == 0


async def test_reaper_requeues_stale_lease_and_kills_exhausted(session_factory, settings):
    async with session_factory() as session:
        stale_pending = (
            await session.execute(
                text(
                    """
INSERT INTO jobs (topic, state, attempts, max_attempts, heartbeat_at, locked_by, run_after)
VALUES ('pipeline.score', 'running', 1, 5, now() - interval '1 hour', 'w', now())
RETURNING id
"""
                )
            )
        ).scalar_one()
        exhausted = (
            await session.execute(
                text(
                    """
INSERT INTO jobs (topic, state, attempts, max_attempts, heartbeat_at, locked_by, run_after)
VALUES ('pipeline.score', 'running', 5, 5, now() - interval '1 hour', 'w', now())
RETURNING id
"""
                )
            )
        ).scalar_one()
        await session.commit()

    reaped = await reap_once(session_factory, settings)
    outcomes = dict(reaped)

    assert outcomes[stale_pending] == "pending"
    assert outcomes[exhausted] == "dead"

    requeued = await _job_row(session_factory, stale_pending)
    assert requeued["state"] == "pending"
    assert requeued["locked_by"] is None
    assert requeued["last_error"] == "lease expired"
    assert requeued["attempts"] == 1

    killed = await _job_row(session_factory, exhausted)
    assert killed["state"] == "dead"
    assert killed["finished_at"] is not None


async def test_two_workers_never_claim_the_same_job(session_factory, settings):
    queue = PgQueue(session_factory, settings)
    total = 50
    for _ in range(total):
        await queue.enqueue("pipeline.score", {})

    claimed: list[int] = []
    lock = asyncio.Lock()

    async def claimer(worker_id: str) -> None:
        while True:
            job = await queue.claim(["pipeline.score"], worker_id, 120)
            if job is None:
                return
            async with lock:
                claimed.append(job.id)

    # 2 worker logici × 4 di concorrenza ciascuno = 8 claimer concorrenti.
    await asyncio.gather(*(claimer(f"worker-{index // 4}-{index % 4}") for index in range(8)))

    assert len(claimed) == total
    assert len(set(claimed)) == total


async def test_rate_limiter_serializes_bucket(session_factory):
    limiter = RateLimiter(session_factory)

    async def acquire() -> float:
        start = time.monotonic()
        await limiter.acquire("test:default", requests_per_minute=2, window_s=1)
        return time.monotonic() - start

    start = time.monotonic()
    elapsed = sorted(await asyncio.gather(*(acquire() for _ in range(4))))

    assert elapsed[0] < 0.5  # due acquisizioni immediate
    assert elapsed[1] < 0.5
    assert elapsed[-1] >= 0.8  # le altre due attendono la fine della finestra
    assert time.monotonic() - start >= 0.8

    async with session_factory() as session:
        used = (
            await session.execute(
                text("SELECT used FROM rate_limits WHERE bucket = :b"),
                {"b": "test:default"},
            )
        ).scalar_one()
    assert 1 <= used <= 2


@pytest.mark.parametrize("bad_topic", ["pipeline.unknown"])
async def test_enqueue_rejects_unknown_topic(session_factory, settings, bad_topic):
    """Il CHECK di ``jobs.topic`` rifiuta i topic non canonici (§6.3)."""
    queue = PgQueue(session_factory, settings)
    with pytest.raises(IntegrityError):
        await queue.enqueue(bad_topic, {})
