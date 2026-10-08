"""Endpoint del catalogo: /ideas, dettaglio, ricerca, watch, merge, delete."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ideai.api import repositories as repo
from ideai.api.deps import get_gateway, get_queue, get_session, require_api_key
from ideai.api.schemas import (
    IdeaDetailResponse,
    IdeaListItem,
    IdeaListResponse,
    MergeRequest,
    ReanalyzeResponse,
    WatchRequest,
)
from ideai.domain import JobTopic, Verdict

router = APIRouter(dependencies=[Depends(require_api_key)])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
QueueDep = Annotated[object, Depends(get_queue)]
GatewayDep = Annotated[object, Depends(get_gateway)]


@router.get("/ideas", response_model=IdeaListResponse)
async def list_ideas(
    session: SessionDep,
    min_score: Annotated[int | None, Query()] = None,
    max_score: Annotated[int | None, Query()] = None,
    verdict: Annotated[Verdict | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    source_kind: Annotated[str | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    sort: Annotated[
        Literal["opportunity_score", "last_activity_at", "first_seen_at"], Query()
    ] = "opportunity_score",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    total, items = await repo.list_ideas(
        session,
        min_score=min_score,
        max_score=max_score,
        verdict=verdict.value if verdict else None,
        status=status,
        category=category,
        tag=tag,
        source_kind=source_kind,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "items": items}


@router.get("/ideas/search", response_model=IdeaListResponse)
async def search_ideas(
    request: Request,
    session: SessionDep,
    q: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    """Ricerca ibrida full-text + vettoriale con fusione RRF."""
    settings = request.app.state.settings
    vectors = await request.app.state.llm.embedder().embed(model=settings.embed_model, texts=[q])
    vec = "[" + ",".join(repr(float(v)) for v in vectors[0]) + "]"
    items = await repo.search_ideas(session, q=q, vec=vec, model=settings.embed_model, limit=limit)
    return {"total": len(items), "items": items}


@router.get("/ideas/{idea_id}", response_model=IdeaDetailResponse)
async def get_idea(idea_id: int, session: SessionDep) -> dict:
    idea = await repo.get_idea(session, idea_id)
    if idea is None:
        raise HTTPException(status_code=404, detail="Idea non trovata")
    return {
        "idea": idea,
        "current_analysis": await repo.current_analysis(session, idea_id),
        "revisions": await repo.list_revisions(session, idea_id),
        "items": await repo.list_idea_items(session, idea_id),
        "timeline": await repo.list_timeline(session, idea_id),
    }


@router.post("/ideas/{idea_id}/watch", response_model=IdeaListItem)
async def set_watch(idea_id: int, body: WatchRequest, session: SessionDep) -> dict:
    if not await repo.idea_exists(session, idea_id):
        raise HTTPException(status_code=404, detail="Idea non trovata")
    await repo.upsert_watch(
        session,
        idea_id,
        enabled=body.enabled,
        mode=body.mode.value,
        interval_s=body.interval_s,
    )
    idea = await repo.get_idea(session, idea_id)
    assert idea is not None
    return idea


@router.post("/ideas/{idea_id}/reanalyze", response_model=ReanalyzeResponse)
async def reanalyze(idea_id: int, session: SessionDep, queue: QueueDep) -> dict:
    if not await repo.idea_exists(session, idea_id):
        raise HTTPException(status_code=404, detail="Idea non trovata")
    job_id = await queue.enqueue(
        JobTopic.ANALYZE,
        {"idea_id": idea_id, "reason": "manual"},
        dedup_key=f"analyze:{idea_id}",
        priority=50,
    )
    return {"job_id": job_id}


@router.post("/ideas/{idea_id}/merge", response_model=IdeaListItem)
async def merge_idea(
    idea_id: int, body: MergeRequest, session: SessionDep, queue: QueueDep
) -> dict:
    if body.into_id == idea_id:
        raise HTTPException(status_code=422, detail="Un'idea non può essere fusa in sé stessa")
    if not await repo.idea_exists(session, idea_id):
        raise HTTPException(status_code=404, detail="Idea sorgente non trovata")
    if not await repo.idea_exists(session, body.into_id):
        raise HTTPException(status_code=404, detail="Idea destinazione non trovata")
    await repo.merge_ideas(session, idea_id, body.into_id)
    await queue.enqueue(
        JobTopic.ANALYZE,
        {"idea_id": body.into_id, "reason": "merge"},
        priority=50,
    )
    idea = await repo.get_idea(session, body.into_id)
    assert idea is not None
    return idea


@router.delete("/ideas/{idea_id}", status_code=204)
async def delete_idea(idea_id: int, session: SessionDep) -> Response:
    if not await repo.delete_idea(session, idea_id):
        raise HTTPException(status_code=404, detail="Idea non trovata")
    return Response(status_code=204)
