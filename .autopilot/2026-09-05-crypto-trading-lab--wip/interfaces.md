# Интерфейсы проекта

## Правила проекта (для каждого исполнителя)

- **Стек:** Python 3.12, `uv` (зависимости в `pyproject.toml`, лок-файл `uv.lock`), Postgres 16 (через `docker compose`), SQLAlchemy 2 + Alembic, FastAPI + Jinja2 + HTMX, aiogram 3, APScheduler 3, Parquet через `pyarrow` + DuckDB, `pydantic` v2 для конфигов/манифестов, `pytest` + `pytest-asyncio`, `ruff`.
- **Команды:** `uv sync` — установка; `uv run pytest -q` — тесты; `uv run ruff check .` — линт; `docker compose up -d` — запуск; `uv run alembic upgrade head` — миграции; `uv run python -m lab <команда>` — CLI.
- **Тесты не ходят в сеть.** Живые API — только за флагом `LAB_LIVE_TESTS=1`, по умолчанию пропускаются. Всё внешнее — за интерфейсом, в тестах — фейк.
- **Секреты только через `.env` по именам из `.env.example`.** Ни один ключ не в коде, не в тестах, не в логах. Ключи с правом вывода средств отклоняются при старте.
- **Недостающая зависимость = вернуть `BLOCKED` с описанием, не устанавливать что попало.** Добавление зависимости в `pyproject.toml` — допустимо, если она есть в этом файле в разделе «Стек» или в тикете.
- **Не трогать:** `.autopilot/`, `CLAUDE.md` (кроме разрешённого в тикете), чужие зоны.
- **Язык:** код и идентификаторы — английский; тексты для пользователя (Telegram, веб, логи уровня INFO) — русский.
- **Время:** в базе UTC; показ пользователю — `Europe/Samara`.
- **Деньги:** `Decimal`, не float. Валюта учёта — USD.
- **Идентификаторы стратегий:** `<ветка>-<источник>-<слаг>` (`hl-copy-0xabc`, `meme-sol-pumpfun-early-v1`, `nft-mint-priority-v2`).
- **Ветки:** `cex-spot`, `cex-perp`, `dex-perp`, `copy`, `meme`, `nft`, `prediction`, `rh`.
- **Ступени:** `backtest → paper → micro → signal → semi → auto`. **Статусы:** `candidate · measuring · passed · failed · degraded · retired`.
- **Каждое решение стратегии пишется в журнал ДО исхода** (`decided_at`, `inputs_hash`).
- **Ни один ордер не уходит на площадку без `core.risk.check(...) == Allow`.**

## Структура репозитория (владение по зонам)

```
src/lab/
  contracts/      # Feed, Executor, Strategy, NftMarket — протоколы и типы (тикет 01)
  core/           # registry, ladder, risk, measure, costs, journal
  data/           # parquet-хранилище свечей/сделок, backfill
  feeds/          # cex/, chains/, dex/, nft/, polymarket/, stocks/
  executors/      # cex/, dex/, nft/, polymarket/, robinhood/
  strategies/     # base, presets/, indicators/, copy/, meme/, nft/, prediction/, external/
  wallets/        # статистика кошельков и лидеров
  nft/            # коллекции, создатели, минты
  discovery/      # поиск кандидатов
  ops/            # scheduler, feeds_registry, jobs, watchdog, backup, reload
  bot/            # Telegram
  web/            # FastAPI
  cli.py          # python -m lab
migrations/       # alembic
config/           # limits.yaml, threshold.yaml, schedule.yaml, authors.yaml, stocks.yaml, external_signals.yaml
docs/research/    # каталоги источников, стратегий, индикаторов
candidates/       # seed.md
tests/
deploy/           # docker-compose.yml, Dockerfile, README по развёртыванию
```

## Границы, решённые в спецификации

| Модуль | Владеет | Выставляет | Прячет |
|---|---|---|---|
| `core.registry` | таблицы strategies/candidates, статусы | `add(spec) -> Strategy`, `get(id)`, `list(filter)`, `retire(id, reason)`, `enqueue_candidate(kind, ref)` | генерацию id, отпечатки дубликатов |
| `core.ladder` | ступени и переходы | `evaluate(strategy_id) -> Transition|None`, `promote(id, by)`, `demote(id, reason)`, `halt_all()`, `resume_all()` | правило порога, историю переходов |
| `core.risk` | лимиты веток, стопы, раскладку, потолок реального капитала | `check(OrderIntent) -> Allow|Deny(reason)`, `allocation(branch) -> Allocation`, `reload()` | арифметику размера, чтение конфига |
| `core.measure` | замеры, метрики, порог | `run(strategy_id, mode, window) -> Measurement`, `metrics(trades, benchmark) -> Metrics`, `threshold(metrics, branch) -> ThresholdResult` | симулятор, бутстрап, бенчмарк |
| `core.costs` | модели издержек по площадкам | `estimate(venue, intent, book|pool) -> Costs`, `actual(fill) -> Costs` | тарифы, формулы проскальзывания |
| `core.journal` | signals/orders/fills/trades, форвард-журнал, сверка | `record_signal(...)`, `record_fill(...)`, `close_trade(...)`, `reconcile(venue)`, `export_csv(filter)` | связывание филлов в сделки |
| `feeds.*` | данные площадок и сетей | контракт `Feed`: `candles(instr, tf, from, to)`, `trades(...)`, `book(instr)`, `events(kind) -> stream`, `health() -> Health` | клиентов API, ретраи, квоты |
| `executors.*` | ордера на площадках | контракт `Executor`: `place(intent, mode) -> Order`, `cancel(order_id)`, `positions()`, `fills(since)`, `balance()` | подписи, client_order_id, реконнект |
| `strategies.*` | правила | `Strategy.manifest`, `on_bar(bar) -> [Signal]`, `on_event(evt) -> [Signal]` | внутреннее состояние правила |
| `discovery` | поиск кандидатов | `scan() -> [Candidate]` | парсеры и дедупликацию |
| `wallets` | статистика кошельков/лидеров | `recalc(address, chain) -> WalletStats`, `flags(address) -> [Flag]`, `lag_cost(address) -> LagCost` | индексацию сделок |
| `nft` | коллекции, создатели, минты | `track(collection)`, `creator_score(creator) -> Score`, `upcoming() -> [Mint]`, `mint(attempt_spec) -> MintResult` | парсинг календарей, специфику площадок |
| `ops.scheduler` | расписание | `register(job)` | APScheduler |
| `ops.feeds_registry` | квоты, здоровье, бюджет | `use(feed_id, n)`, `status() -> [FeedStatus]`, `budget() -> Budget` | подсчёт прогноза |
| `bot` | Telegram | `send_card(kind, payload) -> msg_id`, `morning_report()`, команды | aiogram, outbox |
| `web` | HTTP-экраны | `/`, `/strategies`, `/strategies/{id}`, `/feeds`, `/queue`, `/graveyard` | шаблоны |

### Типы контрактов (тикет 01 фиксирует их в `contracts/`)

```python
class OrderIntent(BaseModel):
    strategy_id: str; venue: str; instrument: str; side: Literal["buy","sell"]
    qty: Decimal; price: Decimal | None; order_type: Literal["market","limit"]
    leverage: Decimal = Decimal(1); reduce_only: bool = False
    mode: Literal["paper","live"]; signal_id: str; client_order_id: str

class Signal(BaseModel):
    strategy_id: str; decided_at: datetime; instrument: str; side: str
    size: Decimal; price_ref: Decimal | None; inputs_hash: str; ttl_s: int; meta: dict

class Costs(BaseModel):
    fee: Decimal; slippage: Decimal; funding: Decimal; gas: Decimal; royalty: Decimal
    @property total

class Health(BaseModel): status: Literal["ok","degraded","down"]; detail: str; checked_at: datetime

class Feed(Protocol):      # см. таблицу
class Executor(Protocol):  # см. таблицу; paper и live — один класс, режим — параметр
class Strategy(Protocol):  # manifest: StrategyManifest; on_bar; on_event
```

**Швы для тестов — три:** `Executor` в режиме `paper` (один контрактный набор `tests/contracts/test_executor_contract.py`, параметризованный по всем исполнителям), `core.risk.check`, `core.measure.run` на синтетических данных из `tests/fixtures/synthetic.py`.

## Что построили тикеты

_(заполняется по мере сдачи тикетов: фактические сигнатуры, отклонения от плана, D##)_
