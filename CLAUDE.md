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

## Эксплуатация

| Что | Команда |
|---|---|
| Развернуть на VPS | `docker compose -f deploy/docker-compose.yml up -d --build` |
| Обновить | `git pull && docker compose -f deploy/docker-compose.yml up -d --build` (миграции автоматом) |
| Логи сервиса | `docker compose -f deploy/docker-compose.yml logs -f worker` (или `bot`, `web`) |
| Замер стратегии руками | `uv run python -m lab measure run <strategy_id> [--mode M] [--days N]` |
| Что уже замерено | `uv run python -m lab measure show <strategy_id>` |
| Доступность площадок | `uv run python -m lab ops status` |
| Источники: квоты, здоровье, бюджет | `uv run python -m lab ops feeds` |
| Резервная копия сейчас | `uv run python -m lab ops backup [--dest DIR] [--keep-days N]` |
| Восстановление | `scripts/restore.sh backups/lab-ГГГГ-ММ-ДД.tar.gz "postgresql://lab:…@localhost:5432/lab"` |
| Перезагрузить конфиги | `uv run python -m lab ops reload` или `docker compose ... kill -s HUP worker` |
| Запустить сервис | `uv run python -m lab service worker` (или `bot`, `web`; `--once` — проверка) |

Расписание — `config/schedule.yaml` (Europe/Samara): бэкап 03:00, сверка с площадками
04:00, утренний отчёт 09:00, поиск кандидатов пн 06:00, переизмерение вс 22:00.
Watchdog шлёт `alert` в Telegram, если сервис молчит 5 минут. Развёртывание на VPS,
`.env`, обновление и восстановление — `deploy/README.md`.

<!-- autopilot:end -->
