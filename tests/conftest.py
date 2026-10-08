"""Harness dei test: Postgres reale con pgvector su una porta dedicata (§11.4c).

Il container è lo stesso dell'Appendice B (``pgvector/pgvector:pg17``): il
comportamento di ``FOR UPDATE SKIP LOCKED``, degli indici parziali e dell'unicità
di ``dedup_key`` non è simulabile. Se Docker non è disponibile i test che usano il
database **falliscono** con un messaggio esplicito: niente skip silenziosi.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ideai.config import Settings
from ideai.db.schema import metadata

ROOT = Path(__file__).resolve().parent.parent

PG_IMAGE = "pgvector/pgvector:pg17"
PG_CONTAINER = "ideai-test-pg"
PG_PORT = 55432
PG_USER = "ideai"
PG_PASSWORD = "ideai"
PG_DB = "ideai"

# Le variabili obbligatorie dell'Appendice A devono esistere prima di ogni Settings().
os.environ.setdefault("IDEAI_API_KEY", "test-api-key")
os.environ.setdefault("IDEAI_SALT", "test-salt")

DOCKER_MISSING_MESSAGE = (
    "Docker non è disponibile: i test di integrazione richiedono il container "
    f"{PG_IMAGE} (§11.4c). Avvia Docker o esporta IDEAI_TEST_DATABASE_URL verso "
    "un Postgres con pgvector ≥ 0.8.0."
)


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=300, check=False
    )


def _docker_available() -> bool:
    try:
        return _docker("info").returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _wait_for_postgres(deadline_s: int = 90) -> bool:
    import time

    for _ in range(deadline_s):
        probe = _docker("exec", PG_CONTAINER, "pg_isready", "-U", PG_USER, "-d", PG_DB)
        if probe.returncode == 0:
            return True
        time.sleep(1)
    return False


def _start_container() -> None:
    _docker("rm", "-f", PG_CONTAINER)
    started = _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        PG_CONTAINER,
        "-e",
        f"POSTGRES_USER={PG_USER}",
        "-e",
        f"POSTGRES_PASSWORD={PG_PASSWORD}",
        "-e",
        f"POSTGRES_DB={PG_DB}",
        "-p",
        f"127.0.0.1:{PG_PORT}:5432",
        PG_IMAGE,
    )
    if started.returncode != 0:
        raise RuntimeError(f"avvio del container fallito: {started.stderr.strip()}")
    if not _wait_for_postgres():
        raise RuntimeError("Postgres di test non è diventato pronto entro 90 secondi")


def _alembic_upgrade(url: str) -> None:
    alembic = Path(sys.executable).parent / "alembic"
    env = {**os.environ, "IDEAI_DATABASE_URL": url}
    result = subprocess.run(
        [str(alembic), "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head fallito:\n{result.stdout}\n{result.stderr}")


@pytest.fixture(scope="session")
def pg_url() -> str:
    """URL async del Postgres di test, con schema già migrato."""
    explicit = os.environ.get("IDEAI_TEST_DATABASE_URL")
    if explicit:
        _alembic_upgrade(explicit)
        os.environ["IDEAI_DATABASE_URL"] = explicit
        yield explicit
        return

    if not _docker_available():
        pytest.fail(DOCKER_MISSING_MESSAGE, pytrace=False)

    _start_container()
    url = f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@127.0.0.1:{PG_PORT}/{PG_DB}"
    try:
        _alembic_upgrade(url)
    except Exception:
        _docker("rm", "-f", PG_CONTAINER)
        raise
    os.environ["IDEAI_DATABASE_URL"] = url
    yield url
    if os.environ.get("IDEAI_TEST_KEEP_PG") != "1":
        _docker("rm", "-f", PG_CONTAINER)


@pytest_asyncio.fixture
async def db_engine(pg_url: str):
    engine = create_async_engine(pg_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    """Session factory con database ripulito prima di ogni test."""
    tables = ", ".join(t.name for t in reversed(metadata.sorted_tables))
    async with db_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
def settings(pg_url: str) -> Settings:
    return Settings(database_url=pg_url)
