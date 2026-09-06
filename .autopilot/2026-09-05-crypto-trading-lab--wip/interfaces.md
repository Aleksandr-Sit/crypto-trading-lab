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

## Из таска 01 — каркас

- `lab.contracts`: типы `OrderIntent, Signal, Costs(.total), Health, KeyRights(trade, withdraw), Order, Fill, Position, Balance, Candle, Trade, Book, Event, StopSpec, StrategyManifest(slug, branch, venue, source_kind, source_ref, instruments, timeframe, params, can_backtest, stop, valid_until, description)`; enums `Branch, Rung, Status, Mode, MeasureMode, SignalOutcome, CandidateDecision, OrderState`; протоколы `Feed, Executor(rights/place/cancel/positions/fills/balance/health), Strategy, NftMarket(upcoming/floor/mint/estimate_costs/health)`.
- `lab.core.registry.Registry(session)`: `add(spec|StrategyManifest) -> Strategy`, `get(id)`, `list(branch=, status=, rung=, venue=)`, `retire(id, reason)`, `enqueue_candidate(kind, ref, payload=None) -> Candidate`, `candidates(decision=None)`; исключения `DuplicateStrategy(existing_id)`, `IncompleteManifest(fields, draft_id)`, `StrategyNotFound`. id = `<branch>-<source_kind>-<slug>`; отпечаток = sha256(branch, venue, source_kind, source_ref, instruments, timeframe, params, stop).
- `lab.executors.registry`: `register(name, factory, replace=False)`, `get(name)`, `all() -> dict[str, factory]`, `unregister(name)`; `lab.executors.FakeExecutor(mark_price, start_balance, quote_asset)`. **Каждый новый исполнитель регистрируется здесь — контрактный тест `tests/contracts/test_executor_contract.py` параметризован по `all()`.**
- `lab.config`: `load_config(path, Model)`, `load_limits/load_threshold/load_schedule()`, `LimitsConfig(.group_of(branch), .for_branch(branch), .real_capital_cap_usd)`, `ThresholdConfig`, `ScheduleConfig`, `ConfigError`, `venues_report(env=None, rights=None) -> [VenueStatus]`, `environment()`, `load_dotenv(path)`, `VENUE_ENV`, `DATA_ENV`.
- `lab.db`: `Base`, `make_engine(url=None)`, `make_session_factory(engine)`, `session_scope(factory)`, `database_url()`; модели `lab.db.models.*Row` (16 таблиц по схеме спецификации). Миграции — Alembic, владелец схемы — таск 01; новые таблицы — новой миграцией в `migrations/versions/`.
- CLI: `python -m lab venues | strategy add/list/retire | candidate add | service worker|bot|web [--once]`.
- Тесты: `uv run pytest -q`; один файл — `uv run pytest -q tests/<path>`; база тестов — локальный Postgres 16 (`TEST_DATABASE_URL`, роль `lab`/`lab`, база `lab_test`); фикстуры `session`, `migrated_engine`, `db_url`; `tests.fixtures.synthetic.synthetic_candles(n, kind, drift_pct, noise_pct, seed, tf, start)`.
- Зависимости, добавленные сверх списка: `psycopg[binary]`, `pyyaml`. `.env` читается своим парсером `lab.config.env.load_dotenv` — не добавляй python-dotenv.
- Docker-демона в среде сборки нет: compose проверен только `config`; сервисы worker/bot/web — заглушки с heartbeat-файлом.

## Из таска 02 — измерение

- `core.costs.CostModel(config)`: `estimate(venue, intent, book=None, pool: Pool|None, depth: Depth|None, *, royalty_pct=None) -> Costs`; `actual(fill, *, side, ref_price=None, funding=, gas=, priority_fee=, royalty=) -> Costs`; `.version = costs-v1@<sha12>`; `funding_interval_h(venue)`; модульные `estimate/actual/default_model/load_costs`; `Pool(liquidity_quote, price, fee_bps=None)`, `Depth(depth_usd, spread_bps=None)`; тарифы — `config/costs.yaml`.
- `core.measure.run(strategy_id, mode, (from, to), *, strategy, candles=|source=, benchmark=, trades=[ClosedTrade] (micro/forward), session=None, costs=, capital=Decimal(10000), walk_forward=(is, oos), params=, rung=, depth=, funding_rate=, seed=0, extra_metrics=) -> Measurement(id, status ok|incomplete, reason, data_hash, code_version, costs_version, params, metrics, threshold, folds, trades, cached)`. Снимок в `measurements.metrics_json`.
- `core.measure.metrics(trades, benchmark, *, capital, window, config=, seed=, extra=) -> Metrics` (`sample: SampleStatus(status, n, required, detail)`; ветко-специфичные — `NotApplicable(reason)`, передаются через `extra_metrics`); `threshold(metrics, branch, *, rung=, config=) -> ThresholdResult(status passed|failed|insufficient, criteria[Criterion(name, value, limit, op, passed, detail)], sample, failed_names())`.
- `PaperEngine(venue, instrument, tf, branch=, costs=, funding_rate=, max_participation=0.1, depth=, on_fill=)`: `.submit(signal)` (LookaheadError), `.on_bar(bar) -> [Fill]`; `simulate(strategy, candles, *, engine) -> SimResult`; `walk_forward_windows(...)`; `measure_plan(manifest|branch) -> MeasurePlan(can_backtest, measure_cost, reason)`; `data_hash`, `code_version`. Лимитка через `Signal.meta={"order_type":"limit","limit_price":...}`.
- `core.journal.Journal(session)`: `record_signal(signal, signal_id=None)`, `record_order(intent, *, order_id, state=, risk_verdict=)`, `record_fill(fill, *, costs=None, ref_price=None) -> [закрытые TradeRecord, FIFO]`, `close_trade`, `open_trades/closed_trades(strategy_id)`, `pnl(strategy_id, *, marks) -> PnL(realized, unrealized, total)`, `reconcile(venue, fills_from_venue, *, since, tolerance) -> ReconcileReport(ok, mismatches, event)`, `export_csv(...) -> str`, `set_outcome(signal_id, outcome, at)`.
- `data.CandleStore(root)`: `write(venue, instr, tf, candles) -> WriteResult` идемпотентно; `read(venue, instr, tf, from, to) -> [Candle]`; `count`, `last_ts`, `query(sql, ...)` (DuckDB); раскладка `root/candles/venue=…/instrument=…/tf=…/YYYY-MM.parquet`. `data.backfill(store, source, venue, instr, tf, from, to, *, chunk=7d, progress=) -> BackfillResult`; `BackfillInterrupted(reason, resume_from, rows_written)`; состояние в `.backfill.json`. **Фиды CEX (таск 04) отдают `source` в виде `(instr, tf, from, to) -> [Candle]`.**
- Миграция 0002: колонки `fills.ref_price/costs_json`, `trades.instrument/venue/mode/side/qty/entry_price/exit_price`. Зависимости: `pyarrow`, `duckdb`, `pandas`, `numpy`. `vectorbt` не используется.
- Открыто: `paper_vs_live_gap` считается через `extra_metrics` — сравнение бумага/реальность делает тот, у кого есть реальные филлы (таски исполнителей).

## Из таска 03 — риск и лестница

- `core.risk.RiskEngine(strategies: (id)->StrategyInfo|None, portfolio: Portfolio, *, limits=, limits_path=, halt: HaltSwitch=, change_log=)`: `check(OrderIntent) -> Allow | Deny(reason, rule)`; `allocation(branch) -> Allocation(branch, group, share_pct, base_usd, current_usd, exposure_usd, available_usd, max_trade_usd, max_leverage, stale, as_of)`; `reload(by) -> ReloadResult(applied, error, change)`; `registry_lookup(Registry)`; `real_capital_cap(limits)` (env `REAL_CAPITAL_CAP` поверх yaml). Коды Deny: halted · unknown_strategy · strategy_inactive · venue_unavailable · rung_mode · stop_missing · strategy_stop_daily · strategy_stop_dd · branch_stop · no_price · leverage · max_trade · branch_share · real_capital_cap · liquidation.
- `Portfolio` (Protocol, **боевой реализации нет — её делает таск 14/исполнители**): `bank_usd()`, `branch(b) -> BranchState(current_usd, exposure_usd, pnl_day_pct, pnl_week_pct, stale, as_of)`, `venue_available(v)`, `strategy_stats(id) -> StrategyStats(pnl_day_pct, dd_pct, exposure_usd)`, `live_deployed_usd()`, `mark_price(v, instr)`, `liquidation_price(intent)`. `HaltSwitch`: `MemoryHaltSwitch`, `DbHaltSwitch(session)` (таблица `system_flags`); `DbConfigLog(session)` (таблица `config_changes`).
- `core.ladder.Ladder(session, *, threshold, halt=, metrics=, cancel_orders=, notify=)`: `start(id)` (начальная ступень по `can_backtest`), `evaluate(id, metrics=None) -> Transition|None`, `promote(id, by, ...)` (semi→auto только by="operator" → иначе `OperatorRequired`), `demote(id, reason, ...)`, `breach(id, reason, snapshot)` (degraded + cancel_orders), `halt_all(by)/resume_all(by)/halted`, `expire_signals(now) -> [signal_id]`, `history(id)`; `Transition(id, strategy_id, from_rung, to_rung, status, reason, by, metrics_snapshot, ts)`; правила — `ladder.rules.*`, `initial_rung(can_backtest)`.
- **Связка «пробой стопа → breach»: `check` возвращает `Deny(strategy_stop_*)`; worker (таск 05/14) при таком Deny вызывает `ladder.breach()`.** `notify` — bot подставляет `send_card("transition")`.
- Миграция 0003: `config_changes`, `system_flags`; `BranchGroupLimits.maintenance_margin_pct`.

### Уточнения после ревью таска 02
- `core.measure.run(..., branch: Branch|str|None=None)` — ветка: явный `branch=` → манифест стратегии → `branch_of_strategy_id(id)` (самое длинное совпадение по `Branch`). `threshold()` даёт `insufficient`, когда критерий не из чего посчитать (нет бенчмарка). `parse_tf`/`TIMEFRAMES` — в `lab.contracts.timeframes`. `pandas` не зависимость. `ttl_s` стратегии — ключ `params["ttl_s"]` манифеста (соглашение, читает ladder).
- **Известный дефект T03 (в concerns, чинить первым):** `risk.check` для `reduce_only` проверяет стопы раньше обхода — закрыть позицию при пробитом стопе нельзя. До фикса worker при `Deny(strategy_stop_*)` на закрывающем ордере должен считать это ошибкой риск-ядра, а не отказом.
