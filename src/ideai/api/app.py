"""Composizione dell'applicazione FastAPI (§9.1)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ideai import __version__
from ideai.api.deps import require_api_key
from ideai.api.routes import health, ideas, jobs, sources, stats
from ideai.config import Settings
from ideai.db.session import create_engine_and_session
from ideai.logging import configure_logging

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings)

    from ideai.adapters.llm import build_transports
    from ideai.adapters.llm.gateway import LlmGateway
    from ideai.runtime.ratelimit import RateLimiter

    engine, session_factory = create_engine_and_session(settings)
    transports = build_transports(settings)
    limiter = RateLimiter(session_factory)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.transports = transports
    app.state.rate = limiter
    app.state.llm = LlmGateway(settings, session_factory, transports, limiter)
    try:
        yield
    finally:
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="IdeaI API", version=__version__, lifespan=lifespan)
    app.state.settings = resolved

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(ideas.router, prefix=API_PREFIX)
    app.include_router(sources.router, prefix=API_PREFIX)
    app.include_router(jobs.router, prefix=API_PREFIX)
    app.include_router(stats.router, prefix=API_PREFIX)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(_: None = Depends(require_api_key)) -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
