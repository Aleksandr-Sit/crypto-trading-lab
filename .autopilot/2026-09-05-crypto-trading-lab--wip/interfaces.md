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

## Из таска 13 — каталоги

- Карточка стратегии: `docs/research/strategies/<branch>-<slug>.md`, YAML-фронтматтер `{id, branch, source_kind, source_ref, can_backtest, timeframe, instruments, venue, regime, status, params{snake_case}}` + секции Идея / Правила (Вход, Выход, Размер, Таймфрейм, Издержки) / Режим / Источники / Чего не хватает. 30 карточек, все `status: hypothesis`; 25 `can_backtest`. **Таск 07 кодирует стратегии по этим карточкам; константы, оставшиеся в прозе (copy-okx-lead-filtered, cex-perp-funding-arb-spot-hedge), выносить в `params` при кодировании.**
- `candidates/seed.md`: файловый фронтматтер `format: candidates-seed/v1`; каждый кандидат — fenced ```yaml `{kind, ref, venue, chain, branch, source_url, found_at, note, priority}`; дубликат = (venue, ref). 43 кандидата. HL-лидеры и смарт-мани кошельки не собраны — discovery (T12) собирает сам.
- `config/authors.yaml`: `authors: [{id, name, aliases, kind, note, channels: [{platform, url, verified_at?, note?}]}]`; pifagor → t.me/pifagortrade, coinmetrika → t.me/CoinMetrika (подтверждено пользователем 2026-09-06); cryptosmx — `[ССЫЛКА — впиши]`.
- `docs/research/indicators/{pifagor,coinmetrika}.md`: v0-описания; у Coinmetrika — 8 индикаторов по названиям/дефолтам со скриншотов пользователя (Pine-кода нет). Эталон для v1 — даты сигналов со скриншотов (±1 мес.), ряд BTC/USD с 2012 (Bitstamp), не Binance.

## Из таска 04 — CEX и Hyperliquid

- `lab.feeds.quota`: протокол `FeedsRegistry.use(feed_id, n=1)` (интерфейс `ops.feeds_registry` из тикета 01), `NullQuota`, `MemoryFeedsRegistry(.used[feed_id], .calls[feed_id])`; алиасы `QuotaSink`, `CountingQuota`. **Таск 14: реальный `ops.feeds_registry` подставляется параметром `quota=` в `make_feed`/`make_executor` — интерфейс не менять.** Каждый сетевой вызов площадки идёт через `use(venue, weight)` (HL — weight 20).
- `lab.feeds.cex`: один класс `CexFeed(transport, *, quota=, page_limit=, degraded_after_s=2)` + подклассы `BybitFeed/OkxFeed/BinanceFeed/HyperliquidFeed`, `FEEDS`, `make_feed(venue, transport=None, *, quota=None)`. Методы: `candles(instr, tf, from, to)` (пагинация по `VenueSpec.page_limit`), `trades(instr, from, to)`, `book(instr, depth=50)`, `ticker(instr) -> {bid, ask, last}`, `funding(instr) -> FundingRate(instrument, rate, next_at, mark_price)`, `events(kind: funding|ticker, *, instruments, poll_s) -> AsyncIterator[Event]` (опрос REST), `health()` — `down` с текстом при гео-блоке (`PermissionDenied`/маркеры «restricted») и обрыве; `source()` — функция для `data.backfill`.
- `lab.feeds.cex.transport`: `Transport` (Protocol: подмножество ccxt + `id`, `has_keys`, `key_rights() -> {trade, withdraw}`), `CcxtTransport(venue, credentials=None, **options)` — ключи из env по `VENUE_ENV`, права ключа реально запрашиваются у площадки (Bybit `v5/user/query-api`, Binance `sapi/account/apiRestrictions`, OKX `account/config`; HL — агентский ключ, withdraw невозможен by design); `credentials_from_env(venue, env=None)`; `VENUE_SPECS[venue] -> VenueSpec(id, page_limit, quote_asset, weight, max_leverage, ...)`, `VENUES`. Инструменты — символы ccxt: спот `BTC/USDT`, перп `BTC/USDT:USDT` (HL: `BTC/USDC:USDC`).
- `lab.feeds.cex.fake.FakeTransport(venue, *, has_keys=True, rights=, taker_bps=10, max_leverage=, balance=, default_mid=)` — площадка в памяти на структурах ccxt: `seed_ohlcv`, `set_ticker`, `set_funding`, `add_funding_payment`, `fill_open_orders()`, сбои `offline`, `timeout_after_accept`, `fail_next`, `ohlcv_fail_after`; `calls` — журнал вызовов. Исключения — настоящие классы ccxt.
- `lab.executors.cex`: один класс `CexExecutor(transport, *, mode="paper"|"live", costs=CostModel, quota=, paper_balance=10000)` + `Bybit/Okx/Binance/HyperliquidExecutor`, `EXECUTORS`, `make_executor(venue, transport=None, *, mode="paper", quota=None)`, `make_transport(venue)`; зарегистрированы в `executors.registry` как `bybit|okx|binance|hyperliquid` (фабрика без аргументов → paper; `FakeTransport` только при явной `LAB_CEX_TRANSPORT=fake` — её ставит `tests/conftest.py`, иначе `CcxtTransport` с ключами из `.env`). **Режим экземпляра разделяет состояние: `positions/fills/balance` paper-экземпляра — из памяти (расчёт), live-экземпляра — только с площадки; `place` с чужим режимом → `CexError`. Worker держит по экземпляру на режим.** `balance()` live при обрыве — последний успешный ответ со `stale=True`, без кэша — `ccxt.NetworkError` наружу; без ключей — `[]`. Paper-филлы — `PaperFill(Fill)` с `ref_price` (мид стакана) для `Journal.record_fill(..., ref_price=)`. `max_leverage(instr)` — из `market()['limits']['leverage']['max']`; неизвестен → 1 (плечо >1 отклоняется). Контракт `Executor` целиком; `place(intent, mode)`: `paper` — живой стакан площадки, VWAP по стакану, комиссия из `core.costs.estimate(venue, intent, book)`; `live` — ccxt `create_order` с `clientOrderId`; после `NetworkError/RequestTimeout` ордер ищется на площадке по client id (дублей нет), повтор того же `client_order_id` возвращает известный ордер. Плечо выше `max_leverage(instr)` площадки → `Order(state=REJECTED, reason=...)` без отправки (лимит ветки — раньше, в `risk.check`).
- `client_order_id(signal_id, venue) -> str` — sha256 от `venue:signal_id`: ≤32 alnum (OKX), HL — `0x`+32 hex (cloid). **Worker кладёт его в `OrderIntent.client_order_id`.**
- Расширения: `rights() -> KeyRights` (пустой ключ → `trade=False`, `withdraw=False`; live при этом → `NotConnected`; ключ с withdraw → `KeyRejected`); `perp_info(instr) -> PerpInfo(instrument, leverage, max_leverage, funding_rate, next_funding_at, mark_price, liquidation_price)` — ликвидация из `fetch_positions` площадки; `funding_payments(since) -> [FundingPayment(id, instrument, amount, ts)]`; `reconcile(since) -> ReconcileResult(orders, fills, positions)` — после обрыва; `restore(session) -> [Order]` — при старте: активные live-ордера из `orders` сверяются с площадкой, состояние пишется обратно в строку; `open_orders()`, `max_leverage(instr)`. Исключения: `CexError`, `NotConnected`, `KeyRejected`.
- `lab.data.backfill_cex.backfill_venue(store, venue, symbols, tf, days, *, feed=, transport=, quota=, archive=, now=, chunk=, progress=(instr, done, total)) -> [SymbolResult(venue, instrument, tf, result: BackfillResult|None, error, resume_from, rows_written)]`; обрыв по символу не роняет остальные, повтор продолжает с `resume_from` (состояние `.backfill.json` из таска 02). `BinanceArchive(fetch=None, *, quota=)`: полные месяцы из `data.binance.vision` (spot / futures um, микросекундные open_time и заголовок в новых архивах учтены), текущий месяц — REST; `url(instr, tf, month)`, `source(rest, now=)`.
- CLI: `lab data backfill --venue bybit --symbols BTC/USDT:USDT[,ETH/USDT:USDT] --tf 1h --days 365 [--root data]` — прогресс в stdout, код 1 при прерывании с подсказкой повторить.
- Отклонения/оговорки: `feeds.cex.{bybit,…}` — классы в одном модуле, не отдельные модули; таблицы фандинга в схеме нет — платежи фандинга пишутся в журнал через `Journal.record_fill(fill, costs=Costs(funding=...))` (worker/таск 14); `events()` — опрос REST, не WebSocket; `hyperliquid-python-sdk` не понадобился (ccxt покрывает свечи/ордера/филлы/фандинг); `python-multipart`/`aiogram`/`fastapi` в pyproject — от тасков 05/06.

## Из таска 05 — бот и планировщик

- `lab.bot.TraderBot(*, session_factory, admin_id, transport, ladder_factory, chat_id=, feeds_status=, on_confirm=(signal_id), on_candidate/on_rebalance/on_allowlist=(ref_id, decision), clock=, confirm_ttl=60s, outbox_options=)`: `await send_card(kind, payload) -> outbox_id`, `send_card_sync`, `notify_transition(Transition)` (для `Ladder(notify=...)`), `await alert_service_down(service, *, silent_for_s, detail)`, `await handle_command(user_id, text)`, `await handle_callback(user_id, data)`, `await expire_pending(now)`, `morning_report() -> str`, `await send_morning_report()`, `await flush()`, `jobs(scheduler) -> [Job]`.
- Виды карточек и payload: signal{signal_id, strategy_id, rung, instrument, side, size, price_ref, venue, ttl_s}, fill{strategy_id, instrument, side, qty, price, venue, costs, signal_id}, transition=`Transition.model_dump()`, candidate{candidate_id, kind, ref, summary}, rebalance{proposal_id, from_branch, to_branch, amount_usd, detail}, allowlist{request_id, address, chain, reason}, alert{service, silent_for_s, detail, at|title}. callback_data: `sig:<id>:executed|skipped|confirm|reject`, `cand|rebal|allow:<id>:<decision>`, `confirm|cancel:<token>`.
- `lab.bot.report.FeedsStatus` (Protocol `status() -> [FeedStatus(feed_id, health, detail, quota_used, quota_limit)]`) — **таск 14 подставляет**. `lab.bot.telegram`: `TelegramTransport(aiogram.Bot)`, `build_dispatcher(bot)`, `run_bot(bot, aio, *, scheduler=, heartbeat=)`, `make_aiogram_bot(token)`.
- `lab.ops.scheduler.Job(id, func, cron=None, description, args, kwargs)`, `Scheduler(config=, timezone=)`: `register(job)` (cron по id из `schedule.yaml`, `enabled: false` — не ставится), `jobs()`, `next_run`, `start/shutdown/run_now/fire`; `default_scheduler()`.
- `lab.ops.outbox.Outbox(session, transport, *, base_delay, max_delay, max_attempts)`: `enqueue(OutboxMessage) -> id`, `await flush(now=) -> FlushReport`, `await edit(ref=, msg=)`, `pending`, `dead`; `Transport` Protocol `send/edit`; таблица `outbox` (миграция 0004).
- `lab.ops.stop_watch.StopWatch(risk, ladder, *, on_breach=)`: `guard(intent) -> Verdict` — Deny(strategy_stop_*) на открывающем ордере → `ladder.breach()` один раз; на reduce_only → `RiskCoreError`. **Worker (таск 14) оборачивает `risk.check` этим guard'ом и регистрирует задание `stop_watch` из schedule.yaml.** Исполнение после `on_confirm` и карточка `fill` после филла — worker таска 14.

## Из таска 06 — веб

- `lab.web.create_app(session_scope, *, feeds: FeedsStatusSource|None, auth: (user, pw)|None) -> FastAPI`; `lab.web.serve(session_factory, *, once=False, env=None)`; маршруты `GET / | /strategies?branch&rung&status&sort&dir | /strategies/{id} | /strategies/{id}/trades.csv | /trades.csv | /feeds | /queue?decision | /graveyard?reason` (`HX-Request: true` → фрагмент).
- `lab.web.feeds_source`: `FeedStatus(id, name, kind, health, health_detail, quota_used, quota_limit, quota_period, exhausted_at, cost_month)`, `Budget(month_limit_usd, spent_usd, forecast_usd)`, протокол `FeedsStatusSource.status()/budget()`, `NoFeedsSource` — **таск 14 подставляет `ops.feeds_registry`** через `create_app(feeds=...)`. Сводка «прошло/упало за неделю» читает причины переходов лестницы по префиксу «порог пройден»/«порог не пройден» — не менять тексты причин в ladder. Капитал по веткам — таблица `allocations` (заполняет Portfolio, таск 14).
