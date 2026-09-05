# Один образ для worker / bot / web / migrate; команда задаётся в docker-compose.yml.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY config ./config
COPY examples ./examples
RUN uv sync --frozen --no-dev

RUN mkdir -p /tmp/lab /app/data /app/backups
CMD ["uv", "run", "python", "-m", "lab", "service", "worker"]
