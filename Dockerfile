# Immagine applicativa IdeaI: una sola immagine per migrate, api, worker, scheduler.
FROM python:3.13-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_SYNC=1

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src/ src/
COPY migrations/ migrations/
COPY alembic.ini pyproject.toml README.md ./

ENTRYPOINT ["uv", "run", "ideai"]
