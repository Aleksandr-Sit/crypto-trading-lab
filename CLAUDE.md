<!-- autopilot:start -->
# crypto-trading-lab

Лаборатория торговых стратегий: сбор данных, каталог гипотез и измерение
доходности по крипте (спот и плечо), мем-коинам, NFT и копитрейдингу.
Ничего не принимается на веру — только бэктест и форвард-замер.

## Команды

| Что | Команда |
|---|---|
| Установка | `uv sync` |
| Тесты | `uv run pytest -q` |
| Один файл | `uv run pytest -q tests/<path>` |
| Линт | `uv run ruff check .` |
| Миграции | `uv run alembic upgrade head` |
| CLI | `uv run python -m lab <команда>` |
| Запуск | `docker compose -f deploy/docker-compose.yml up -d` |

## Как здесь работает Autopilot

Сборка ведётся навыком `/autopilot`. Требования, спецификация и таски — в `.autopilot/`.
Прогресс — `.autopilot/dashboard.html`. Правило: требование из `manifest.md`
может снять только пользователь.

Если работа продолжается — скажи «продолжи автопилот»: состояние поднимется
из `.autopilot/state.js`, переспрашивать ничего не нужно.
<!-- autopilot:end -->
