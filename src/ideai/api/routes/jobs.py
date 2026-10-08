"""Endpoint operativi: /jobs e retry manuale."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ideai.api import repositories as repo
from ideai.api.deps import get_session, require_api_key
from ideai.api.schemas import JobInfo, JobListResponse

router = APIRouter(dependencies=[Depends(require_api_key)])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    session: SessionDep,
    state: Annotated[str | None, Query()] = None,
    topic: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    total, oldest, items = await repo.list_jobs(session, state=state, topic=topic, limit=limit)
    return {"total": total, "oldest_pending": oldest, "items": items}


@router.post("/jobs/{job_id}/retry", response_model=JobInfo)
async def retry_job(job_id: int, session: SessionDep) -> dict:
    status, job = await repo.retry_job(session, job_id)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Job non trovato")
    if status == "not_dead":
        raise HTTPException(
            status_code=422, detail="Solo i job in stato dead possono essere ritentati"
        )
    assert job is not None
    return job
