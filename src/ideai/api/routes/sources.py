"""Endpoint delle sorgenti e dei target: /sources, /targets."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ideai.api import repositories as repo
from ideai.api.deps import get_session, require_api_key
from ideai.api.schemas import SourceInfo, SourceTargetInfo, TargetCreateRequest, TargetPatchRequest

router = APIRouter(dependencies=[Depends(require_api_key)])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/sources", response_model=list[SourceInfo])
async def list_sources(request: Request, session: SessionDep) -> list[dict]:
    settings = request.app.state.settings

    def limit_for(kind: str) -> int | None:
        return {
            "reddit": settings.reddit_rpm,
            "reddit_pullpush": settings.pullpush_rpm,
        }.get(kind)

    return await repo.list_sources(session, limit_for)


@router.post("/sources/{source_id}/targets", response_model=SourceTargetInfo, status_code=201)
async def create_target(source_id: int, body: TargetCreateRequest, session: SessionDep) -> dict:
    created = await repo.create_target(
        session,
        source_id=source_id,
        target_ref=body.target_ref,
        target_kind=body.target_kind,
        poll_interval_s=body.poll_interval_s,
    )
    if created is None:
        raise HTTPException(status_code=404, detail="Sorgente non trovata")
    if created == "conflict":
        raise HTTPException(
            status_code=409, detail="Esiste già un target con questo target_ref per la sorgente"
        )
    return created


@router.patch("/targets/{target_id}", response_model=SourceTargetInfo)
async def patch_target(target_id: int, body: TargetPatchRequest, session: SessionDep) -> dict:
    updated = await repo.patch_target(
        session, target_id, enabled=body.enabled, poll_interval_s=body.poll_interval_s
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Target non trovato")
    return updated


__all__ = ["Response", "router"]
