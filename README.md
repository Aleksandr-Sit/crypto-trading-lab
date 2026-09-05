# crypto-trading-lab

Лаборатория торговых стратегий: реестр гипотез, лестница ступеней
`backtest → paper → micro → signal → semi → auto`, замеры с издержками и форвард-журнал
по веткам `cex-spot · cex-perp · dex-perp · copy · meme · nft · prediction · rh`.
Ничего не принимается на веру — только бэктест и форвард-замер.

## Как поднять

```bash
uv sync                                   # зависимости (Python 3.12, uv)
cp .env.example .env                      # впиши DATABASE_URL и ключи (без права вывода!)
docker compose -f deploy/docker-compose.yml up -d   # Postgres + worker/bot/web
uv run alembic upgrade head               # схема базы (в compose это делает сервис migrate)
uv run python -m lab venues               # какие площадки подключены
uv run python -m lab strategy add --file examples/strategy.yaml
uv run python -m lab strategy list
```

`DATABASE_URL` берётся из окружения, иначе из `.env` — одинаково для CLI, сервисов и Alembic;
не задан нигде — ошибка с подсказкой, молчаливого localhost нет.

Два рабочих пути к базе в compose:

- **С хоста.** Порт `db` опубликован на `127.0.0.1:5432` (переменная `DB_BIND`), поэтому в `.env`
  `DATABASE_URL=postgresql+psycopg://lab:<POSTGRES_PASSWORD>@localhost:5432/lab` — и
  `uv run python -m lab ...` с хоста ходит в базу контейнера.
- **Внутри контейнера.** `docker compose -f deploy/docker-compose.yml exec worker uv run python -m lab strategy list`
  — там `DATABASE_URL` уже указывает на `db:5432`.

Без docker: локальный Postgres 16 и та же строка `DATABASE_URL` в `.env`.

## Команды

| Что | Команда |
|---|---|
| Установка | `uv sync` |
| Тесты | `uv run pytest -q` (один файл: `uv run pytest -q tests/<путь>`) |
| Линт | `uv run ruff check .` |
| Миграции | `uv run alembic upgrade head` |
| CLI | `uv run python -m lab <команда>` — `venues`, `strategy add/list/retire`, `candidate add`, `service worker|bot|web` |
| Запуск | `docker compose -f deploy/docker-compose.yml up -d` |

Тесты базы ходят в Postgres по `TEST_DATABASE_URL`
(по умолчанию `postgresql+psycopg://lab:lab@localhost:5432/lab_test`); нет базы — пропускаются.
Живые API в тестах — только при `LAB_LIVE_TESTS=1`.

## Где что лежит

| Путь | Что |
|---|---|
| `config/limits.yaml` | раскладка капитала 40/25/20/10/5 и лимиты веток (В9а) |
| `config/threshold.yaml` | порог прохождения ступени (В12) |
| `config/schedule.yaml` | расписание планировщика (Europe/Samara) |
| `.env.example` | все имена секретов с комментариями; значений в репозитории нет |
| `examples/strategy.yaml` | пример манифеста стратегии |
| `src/lab/contracts/` | протоколы `Feed`, `Executor`, `Strategy`, `NftMarket` и типы (`OrderIntent`, `Signal`, `Costs`, `Health`, `KeyRights`, `StrategyManifest`) |
| `src/lab/core/registry.py` | реестр стратегий и очередь кандидатов |
| `src/lab/executors/` | реестр исполнителей и `FakeExecutor` (paper без сети) |
| `src/lab/db/` | SQLAlchemy-модели ядра, engine, сессии |
| `migrations/` | Alembic |
| `deploy/` | docker-compose, Dockerfile, заметки по развёртыванию |
| `tests/fixtures/synthetic.py` | генератор синтетических свечей (тренд/флэт/шум, seed) |

## Правила

- Деньги — `Decimal`; время в базе — UTC; показ — `Europe/Samara`.
- Каждое решение стратегии пишется в журнал до исхода (`decided_at`, `inputs_hash`).
- Ни один ордер не уходит без `core.risk.check(...) == Allow`.
- Ключ с правом вывода средств отклоняется при старте и площадка не используется.
