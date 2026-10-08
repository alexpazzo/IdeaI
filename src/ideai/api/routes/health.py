"""Endpoint di salute: /healthz e /readyz — gli unici senza API key (§9.1)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ideai.api.deps import get_session

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]

MIN_PGVECTOR = (0, 8, 0)


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in raw.split("."):
        digits = "".join(c for c in piece if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, session: SessionDep) -> JSONResponse:
    settings = request.app.state.settings
    checks: dict[str, object] = {}
    missing: list[str] = []

    try:
        version = await session.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        checks["postgres"] = True
        checks["pgvector"] = version
        if not version or _version_tuple(version) < MIN_PGVECTOR:
            missing.append("pgvector>=0.8.0")
    except Exception as exc:  # pragma: no cover - dipende dall'ambiente
        checks["postgres"] = False
        checks["pgvector"] = None
        missing.append(f"postgres: {exc.__class__.__name__}")

    transports = getattr(request.app.state, "transports", None)
    for role, spec in (
        ("triage", settings.triage_model),
        ("analyst", settings.analyst_model),
        ("embed", settings.embed_model),
    ):
        backend = spec.split("/", 1)[0]
        checks[f"backend:{role}"] = backend
        if transports is None or backend not in transports:
            missing.append(f"backend {backend} del ruolo {role}")
            continue
        try:
            await transports[backend].available_models()
        except ValueError:
            checks[f"reachable:{role}"] = "non verificabile"
        except Exception as exc:
            checks[f"reachable:{role}"] = False
            missing.append(f"backend {backend} non raggiungibile ({exc.__class__.__name__})")

    if transports is not None:
        backend = settings.embed_model.split("/", 1)[0]
        if backend in transports:
            try:
                raw_embedding = await transports[backend].embed(
                    model=settings.embed_model.split("/", 1)[1], texts=["prova"]
                )
                dim = len(raw_embedding.vectors[0])
                checks["embed_dim"] = dim
                if dim != settings.embed_dim:
                    missing.append(f"embedding dim {dim} != IDEAI_EMBED_DIM {settings.embed_dim}")
            except Exception as exc:
                checks["embed_dim"] = None
                missing.append(f"embedding non verificabile ({exc.__class__.__name__})")

    if missing:
        return JSONResponse(
            status_code=503, content={"status": "degraded", "checks": checks, "missing": missing}
        )
    return JSONResponse(status_code=200, content={"status": "ok", "checks": checks})
