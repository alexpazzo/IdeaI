"""Endpoint KPI: /stats."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ideai.api import repositories as repo
from ideai.api.deps import get_session, require_api_key
from ideai.api.schemas import StatsResponse

router = APIRouter(dependencies=[Depends(require_api_key)])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/stats", response_model=StatsResponse)
async def get_stats(session: SessionDep) -> dict:
    return await repo.stats(session)
