"""Dipendenze FastAPI: autenticazione, sessione, coda, gateway LLM."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

UNAUTHORIZED_DETAIL = "API key mancante o non valida"


async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Bearer token obbligatorio su ogni endpoint diverso da healthz/readyz (§11.3)."""
    expected = f"Bearer {request.app.state.settings.api_key}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail=UNAUTHORIZED_DETAIL)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Una sessione per richiesta; commit a fine richiesta, rollback su errore."""
    async with request.app.state.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_queue(request: Request):
    from ideai.adapters.queue_pg import PgQueue

    return PgQueue(request.app.state.session_factory)


def get_gateway(request: Request):
    return request.app.state.llm


__all__ = ["get_gateway", "get_queue", "get_session", "require_api_key"]
