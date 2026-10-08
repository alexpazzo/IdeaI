"""Creazione dell'engine async e della session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine_and_session(settings):
    """Ritorna ``(engine, session_factory)`` con pool prudente e pre-ping."""
    engine: AsyncEngine = create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
        future=True,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory
