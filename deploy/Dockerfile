# Один образ для worker / bot / web / migrate; команда задаётся в docker-compose.yml.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# UV_NO_SYNC=1 — на старте контейнера `uv run` не должен лезть в сеть. Без него он
# досинхронизирует окружение с dev-группой (тянет ruff и pytest, ~10 МБ), хотя образ собран
# с --no-dev: старт растягивается на десятки секунд, а при обрыве сети сервис не поднимется.
# На сборку не влияет: `uv sync` ниже вызывается явно. Все рантайм-зависимости — основные,
# dev-группа (pytest, ruff) в контейнере не нужна.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 TZ=UTC UV_NO_SYNC=1
WORKDIR /app

# Версия кода для снимка замера (`core.measure.runner.code_version`). Внутри контейнера
# .git нет, поэтому git rev-parse там не работает — значение передаёт deploy/update.sh
# через build-arg. Не передали — замер запишет "unknown" и не будет притворяться.
ARG LAB_CODE_VERSION=""
ENV LAB_CODE_VERSION=${LAB_CODE_VERSION}

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY config ./config
COPY examples ./examples
# docs — не только документация: карточки стратегий из docs/research/strategies читает
# lab.strategies.presets.cards на ИМПОРТЕ реестра (CARDS_DIR). Без них любая команда,
# собирающая стратегии (measure, worker, discovery), падает в контейнере с
# FileNotFoundError, хотя тесты из репозитория проходят. 280 КБ, кладём целиком.
COPY docs ./docs
# scripts — для сервисов замера вперёд автомата CryptosMX (crash-forward, crash-paper):
# docs/research/crash-bounce-paper-2026-10-09.md. Без них сервисам нечего запускать. ~1 МБ.
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

RUN mkdir -p /tmp/lab /app/data /app/backups
CMD ["uv", "run", "python", "-m", "lab", "service", "worker"]
