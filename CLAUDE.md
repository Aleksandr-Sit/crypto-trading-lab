<!-- autopilot:start -->
# crypto-trading-lab

Личная лаборатория торговых гипотез: собирает данные, кодирует стратегии, меряет их
на истории и вперёд, и по замеру двигает по ступеням от бэктеста к реальным деньгам.
Один пользователь-оператор, Telegram и веб-экран — интерфейс.

## Команды

Все проверены в этой сборке (Python 3.12, `uv`, Postgres 16 локально).

| Что | Команда | Что вышло |
|---|---|---|
| Установка | `uv sync` | 80 пакетов, лок сходится |
| Тесты | `uv run pytest -q` | **524 passed, 29 skipped**, ~20 с |
| Один файл | `uv run pytest -q tests/<путь>` | |
| Линт | `uv run ruff check .` | All checks passed |
| Миграции | `uv run alembic upgrade head` | голова `0011`, одна |
| Какая голова | `uv run alembic heads` / `alembic current` | `0011 (head)` |
| Compose | `docker compose -f deploy/docker-compose.yml config` | ок (демон не нужен, его тут и нет) |
| CLI | `uv run python -m lab --help` | `venues strategy candidate data measure ops service` |
| Ключи площадок | `lab venues` | таблица «подключена / только данные», `.env` не нужен |
| Реестр | `lab strategy list` \| `strategy add --file` \| `strategy retire` | **нужен `DATABASE_URL`** |
| Кандидат | `lab candidate add` | нужен `DATABASE_URL` |
| Свечи | `lab data backfill --venue bybit --symbols BTC/USDT:USDT --tf 1h --days 365` | нужна сеть к бирже |
| Замер | `lab measure run <id> [--mode backtest\|paper\|forward\|micro] [--days N]` | нужен `DATABASE_URL` |
| Снимки замеров | `lab measure show <id> [--limit N]` | «ещё не мерил», если пусто |
| Доступность площадок | `lab ops status` | в песочнице все `недоступна` |
| Источники и квоты | `lab ops feeds` | 22 фида, здоровье и счётчики квот |
| Бэкап | `lab ops backup [--dest DIR] [--keep-days N]` | `lab-ГГГГ-ММ-ДД.tar.gz` (pg_dump + `config/`) |
| Перезагрузка конфигов | `lab ops reload` | «Конфиги перезагружены» |
| Сервис | `lab service worker\|bot\|web [--once]` | worker: 12 заданий; web без `WEB_*` печатает разовый пароль |
| Восстановление | `scripts/restore.sh backups/lab-….tar.gz "postgresql://…"` | |

Везде `lab` = `uv run python -m lab`. Расписание — `config/schedule.yaml`, TZ
`Europe/Samara`: бэкап 03:00, сверка 04:00, отчёт 09:00, поиск кандидатов пн 06:00,
переизмерение вс 22:00, срок годности 22:15, предложение перелива 22:30.
Развёртывание на VPS — `deploy/README.md`.

## Где это работает

- **Репозиторий:** `github.com/Aleksandr-Sit/crypto-trading-lab`, приватный, ветка `main`.
- **Сервер:** Hostkey `151.244.251.34` (`ssh vps-trader`), каталог `/opt/crypto-trading-lab`.
  Код тянется deploy-ключом **только на чтение** (`~/.ssh/github_lab`, алиас хоста `github-lab`),
  запушить с сервера нельзя — правки делаются локально и приезжают через `git pull`.
- **Обновление:** `cd /opt/crypto-trading-lab && git pull && docker compose -f deploy/docker-compose.yml up -d --build`.
- **Веб:** на сервере `127.0.0.1:8090` — 8080 занят ботом `crypto-trader`. Снаружи только
  туннелем: `ssh -L 8080:127.0.0.1:8090 root@151.244.251.34`, дальше `http://127.0.0.1:8080`.
- **Бот:** `@laboratory63_bot`, отдельный от бота `crypto-trader`. Один токен двумя процессами
  опрашивать нельзя — они выбивают друг друга (`TelegramConflictError`), и пострадает соседний
  проект, а не этот. Свой бот на каждый проект.
- **Соседи на том же сервере:** `crypto-trader` (торгует живьём, health-эндпоинт 8080 смотрит
  watchdog на Senko), `wake`, `smart-money`, `beach-volley-coach`. Память и порты общие:
  перед публикацией порта — `ss -tlnp`, перед пересборкой — `df -h /`.

## Как устроено

`src/lab/*` — 28 тыс. строк. Кто чем владеет:

| Пакет | Владеет | Выставляет |
|---|---|---|
| `contracts` | единственная точка импорта типов | `OrderIntent, Signal, Costs, Health, Order, Fill, Position, Balance, Candle, Trade, Book, Event, StopSpec, StrategyManifest, KeyRights`; enums `Branch/Rung/Status/Mode/MeasureMode/SignalOutcome/CandidateDecision/OrderState`; протоколы `Feed, Executor, Strategy, NftMarket`; `parse_tf` |
| `config` | YAML→pydantic и чтение `.env` | `load_limits/load_threshold/load_schedule`, `load_config`, `environment()`, `venues_report()`, `load_dotenv/apply_dotenv`, `ConfigError` |
| `db` | схема, engine, сессии | `Base`, `make_engine`, `make_session_factory`, `session_scope`, `database_url`, `db.models.*Row` |
| `core.registry` | таблицы strategies/candidates, статусы | `Registry.add/get/list/retire/enqueue_candidate/candidates`, `DuplicateStrategy` |
| `core.ladder` | ступени и переходы | `Ladder.start/evaluate/promote/demote/breach/halt_all/resume_all/expire_signals/history` |
| `core.risk` | лимиты веток, стопы, раскладка, потолок капитала | `RiskEngine.check(intent) -> Allow\|Deny`, `.allocation(branch)`, `.reload(by)`; `Portfolio` — протокол |
| `core.measure` | замер, метрики, порог | `run(...) -> Measurement`, `metrics`, `threshold`, `history`, `PaperEngine`, `simulate`, `walk_forward_windows`, `measure_plan` |
| `core.costs` | издержки площадок (`config/costs.yaml`) | `CostModel.estimate/actual`, `.version` |
| `core.journal` | signals/orders/fills/trades, сверка | `record_signal/record_order/record_fill/close_trade/pnl/reconcile/export_csv/set_outcome` |
| `data` | Parquet-хранилище свечей, бэкфилл | `CandleStore.write/read/query` (DuckDB), `backfill`, `backfill_cex.backfill_venue`, `BinanceArchive` |
| `feeds.cex` | Bybit/OKX/Binance/Hyperliquid через ccxt | `CexFeed` + подклассы, `make_feed`, `CcxtTransport`, `FakeTransport`, `VENUE_SPECS` |
| `feeds.chains` | Solana/EVM/BNB/TON/HL-user | `ChainFeed.wallet_trades/events`, `make_chain_feed`, `HttpxTransport`, `FakeHttpTransport` |
| `feeds.dex` | ранняя стадия мемов: поток новых токенов и миграций | `honesty_check`, `TokenInfo`, `NEW_TOKEN_EVENT/NEW_PAIR_EVENT/MIGRATION_EVENT`, `load_meme` |
| `feeds.nft` | площадки NFT за `NftMarket` | `make_market`, `OpenSea/MagicEden/Tensor/Zora/Blur/Alchemy`, `MintCalendar`, `load_nft` |
| `feeds.polymarket` | рынки, история цен, лидерборд, позиции | `PolymarketFeed`, `pm_position_payload` (инструмент = `token_id`, не рынок) |
| `feeds.robinhood` | крипта Robinhood, подпись Ed25519 | `RobinhoodFeed`, `Ed25519Signer`, `FakeRhTransport` |
| `feeds.stocks` | дневные свечи акций | `StockFeed`, `make_stock_provider` (`STOCK_DATA_PROVIDER`) |
| `feeds.social` | Telegram-каналы авторов, парсер сигналов | `make_reader` (без ключей — `TmePreviewReader` через `t.me/s/`, с `TELEGRAM_API_ID/HASH` — `TelegramReader` на Telethon), `ChannelMessage`, `parser`, журнал `signals_public` |
| `feeds.quota` | шов квот для всех фидов | `FeedsRegistry.use(feed_id, n)`, `NullQuota`, `MemoryFeedsRegistry` |
| `executors.*` | ордера: `cex`, `dex`, `nft` (+минт), `polymarket`, `robinhood` | контракт `Executor`; `executors.registry.register/get/all`; `executors.access.branch_mode` |
| `strategies` | правила | `Strategy` (`on_bar/on_event -> [Signal]`), `strategies.registry.build/manifest/all`, `inputs_hash` |
| `strategies.presets/indicators/meme/nft/copy/prediction/stocks/external` | сами правила по веткам | 10 стратегий в реестре кода (пресеты ботов + индикаторы); `copy`/`meme`/`nft`/`prediction` собираются под конкретного лидера/токен фабриками |
| `wallets` | статистика кошельков и лидеров | `recalc -> WalletStats`, `flags`, `lag_cost`, `save_stats/load_stats/tracked` |
| `nft` | коллекции, создатели, лента минтов, allowlist, позиции | `CollectionTracker`, `creator_score`, `UpcomingFeed`, `attention_index`, `hold_plan`, `nft_costs` |
| `discovery` | поиск кандидатов | `scan() -> ScanResult`, `default_sources`, `decide`, `candidate_hook`, `fingerprint` |
| `ops` | эксплуатация: здесь сходятся все швы | `worker.Worker` (12 заданий), `scheduler`, `feeds_registry`, `portfolio.LivePortfolio`, `stop_watch`, `measure.make_measure`, `outbox`, `backup`, `watchdog`, `reload`, `funding`, `availability`, `jobs/*` |
| `bot` | Telegram | `TraderBot.send_card/handle_command/handle_callback/morning_report`; aiogram — в `bot.telegram`, лениво |
| `web` | HTTP-экраны за Basic | `create_app`, `serve`; `/`, `/strategies`, `/strategies/{id}`, `/feeds`, `/queue`, `/graveyard`, `*.csv` |
| `cli.py` | `python -m lab` | группы `venues strategy candidate data measure ops service` |

Фактические сигнатуры по тикетам и все отклонения от плана —
`.autopilot/2026-09-05-crypto-trading-lab/interfaces.md`. Это единственный файл
из `.autopilot/`, который стоит открывать при работе с кодом.

## Главные понятия

- **Ветка** (`Branch`) — рынок со своими лимитами и порогом: `cex-spot`, `cex-perp`,
  `dex-perp`, `copy`, `meme`, `nft`, `prediction`, `rh`. Группы веток и доли капитала —
  `config/limits.yaml`.
- **Стратегия** — правило: `StrategyManifest` (ветка, площадка, инструменты, `params`,
  `can_backtest`, `stop`, `ttl_s` внутри `params`) плюс `on_bar/on_event -> [Signal]`.
  Id — `<ветка>-<источник>-<слаг>`. Два реестра, не путать: `strategies.registry` — какие
  правила умеет собирать код; `core.registry` — записи со ступенью и статусом в базе.
- **Ступень** (`Rung`): `backtest → paper → micro → signal → semi → auto`. Ниже `micro`
  реальных ордеров нет. `semi → auto` — только рукой оператора (`OperatorRequired`).
- **Статус** (`Status`): `candidate · measuring · passed · failed · degraded · retired`.
  `degraded` разрешает закрывать позиции и запрещает открывать.
- **Замер** (`core.measure.run`) — `Measurement` со снимком метрик, `data_hash`,
  `code_version`, `costs_version`. Режимы: `backtest`, `paper`, `micro`, `forward`.
  Нечего посчитать — `status="incomplete"` с причиной, а не выдуманное число.
- **Порог** (`config/threshold.yaml`) — правило прохождения ступени: ≥30 сделок,
  EV после издержек > 0, MaxDD в лимите ветки, лучше бенчмарка (BTC buy-and-hold).
  Результат — `passed | failed | insufficient`.
- **Издержки** (`core.costs`) — комиссия, проскальзывание, фандинг, газ, роялти;
  считаются и в бумаге, и по факту филла. Замера без издержек не бывает.

## Правила, которые легко нарушить

1. **Ни один ордер не уходит на площадку без `core.risk.check(...) == Allow`.** Путь
   один — `ops.worker.place_signal`. Обёртка `ops.stop_watch.StopWatch.guard` при
   `Deny(strategy_stop_*)` на открывающем ордере переводит стратегию в `degraded`;
   на закрывающем (`reduce_only`) это дефект ядра (`RiskCoreError`), а не отказ —
   закрыть позицию можно всегда.
2. **Решение стратегии пишется в журнал ДО исхода** — `Journal.record_signal` с
   `decided_at` и `inputs_hash`; исход дописывается потом (`set_outcome`). То же в
   `feeds.social.journal` для чужих публичных сигналов.
3. **Деньги — `Decimal`, не float.** Валюта учёта — USD. Время в базе UTC, показ — `Europe/Samara`.
4. **Секреты — только через `.env` по именам из `.env.example`.** Ни ключа в коде, тестах
   или логах; логируется «заполнено / пусто». Ключ с правом вывода средств отклоняется
   при старте (`KeyRejected`).
5. **Тесты не ходят в сеть.** Живые API — за `LAB_LIVE_TESTS=1` (без него 29 тестов
   пропускаются). `tests/conftest.py` ставит `LAB_CEX_TRANSPORT=fake`. Всё внешнее —
   за интерфейсом, в тестах фейк: `FakeTransport`, `FakeHttpTransport`, `FakeRhTransport`,
   `FakeNftMarket`, `FakeExecutor`, `FakeMintClient`.
6. **Режим `paper`/`live` — по экземпляру исполнителя, не смешивать.** У paper-экземпляра
   `positions/fills/balance` считаются в памяти, у live — берутся с площадки; `place` с
   чужим режимом бросает ошибку. Worker держит по экземпляру на режим.
7. **Новый исполнитель регистрируется в `lab.executors.registry`** (в `__init__.py` своего
   пакета, фабрика без аргументов → paper). Контрактный тест
   `tests/contracts/test_executor_contract.py` параметризован по `registry.all()` и
   подхватит его сам. Сейчас там 17 имён (`bybit okx binance hyperliquid jupiter uniswap
   pancake stonfi opensea magiceden tensor zora nft_mint_solana nft_mint_evm polymarket
   robinhood fake`).
8. **Стратегии не импортируют `core.ladder`, `core.risk`, `core.registry`, `executors`** —
   это проверяет `tests/strategies/test_base.py`.
9. **Миграции — только новой ревизией**, голова одна (`0011`). Правка уже выданной ревизии —
   нет; `alembic heads` должен показывать ровно одну строку.

## Окружение

Минимум, чтобы что-то заработало:

- **`DATABASE_URL`** — без него всё, что трогает базу (`lab strategy/candidate/measure/ops/service`),
  падает с `DatabaseUrlMissing`. `lab venues` и `alembic` живут без него: alembic берёт
  запасной URL из `alembic.ini`. Начинать с `cp .env.example .env` — файл копируется
  как есть и даёт рабочую конфигурацию без ключей (все площадки «только данные»).
- **Postgres 16** локально, роль `lab`/`lab`, базы `lab` и `lab_test`. Тесты берут
  `TEST_DATABASE_URL` (по умолчанию `postgresql+psycopg://lab:lab@localhost:5432/lab_test`);
  базы нет — весь набор `skip`, а не падение.
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ADMIN_ID` — без них `lab service bot` печатает, что не
  заданы, и живёт одним heartbeat'ом.
- `WEB_USER`/`WEB_PASSWORD` — без них `lab service web` печатает разовый пароль в лог
  (веб без авторизации не поднимается вообще).
- Ключи площадок — без них ветка честно уходит в «только замер» / «только данные»
  (`BranchMode`, `check_trading_access`), а не притворяется рабочей.
- `CHAINS_ENABLED` — какие сети включены и в каком порядке; неизвестное имя → `ConfigError`.
- `REAL_CAPITAL_CAP`, `BUDGET_MONTH_USD`, `BACKUP_DIR`, `LAB_DATA_ROOT` (по умолчанию `data/`) —
  поверх yaml.

## Грабли

- **Хвостовой комментарий в `.env` доезжает до контейнера как ЗНАЧЕНИЕ.** Свой парсер
  `lab.config.env.load_dotenv` его срезает (вне кавычек), поэтому с хоста всё выглядело
  правильно. Но в compose `.env` идёт через `env_file`, а тот ничего не срезает: строка
  `BYBIT_API_KEY=   # ключ Bybit` кладёт в переменную текст комментария. На сервере так
  «заполнились» 26 ключей, и `lab venues` рапортовал «подключена» по всем площадкам вместо
  честного «только данные» — система считала, что у неё есть ключи всех бирж. Поэтому в
  `.env.example` пояснения стоят ОТДЕЛЬНЫМИ строками над ключом; хвостовые не возвращать.
  Проверка на сервере: `docker compose … exec worker uv run python -m lab venues` — без
  ключей все площадки должны быть «только данные». Не подключать `python-dotenv`: он делает
  это иначе.
- **`migrations/env.py` и `DATABASE_URL` при тестах.** Порядок в `db.engine.alembic_url`:
  `attributes["sqlalchemy.url"]` (программный вызов) → `DATABASE_URL` → `.env` →
  `alembic.ini`. Программный вызов сильнее окружения намеренно: иначе миграции тестов
  уезжают на боевую базу. `tests/conftest.py` на это опирается — не «упрощать».
- **Postgres в песочнице сам не поднимается.** `pg_ctlcluster 16 main start`, проверить
  `pg_lsclusters` (должно быть `online`, порт 5432).
- **Сети к биржам в песочнице нет.** `lab ops status` показывает все площадки
  `недоступна`, Polymarket — `гео-блок (403 Forbidden)`. Это ожидаемо: `lab data backfill`
  и всё живое здесь не проверить. Отказ источника — деградация (`FeedUnavailable` →
  замер `incomplete`), процесс не падает.
- **Живая подпись транзакций не реализована.** `solders`/`web3`/TON-SDK/`py-clob-client`
  в дерево зависимостей не втянуты (тяжёлые, для чтения не нужны). Клиенты грузят их
  лениво и в `live` бросают `TradingUnavailable` с текстом, чего не хватает, — ветка
  уходит в «только замер», а не молчит. Касается `executors.dex`, `executors.nft` (минт),
  `executors.polymarket`.
- **Docker-демона здесь нет** — проверяется только `docker compose ... config`, `up` не
  запускается. Сервисы отлаживать через `lab service <s> --once`.
- **Compose читает `.env` рядом с собой, то есть `deploy/.env`, а не корневой.** Подстановки
  `${WEB_BIND}`, `${DB_BIND}`, `${POSTGRES_PASSWORD}` без ссылки `deploy/.env → ../.env`
  молча уезжают в умолчания: веб встаёт на `127.0.0.1:8080`, пароль базы становится `lab`.
  Отказ тихий — `up` проходит, просто не тот порт. Ссылку делает `deploy/setup.sh`; проверка —
  `docker compose -f deploy/docker-compose.yml config | grep published`. На env_file внутри
  сервисов это не влияет: в контейнеры `../.env` попадает и без ссылки.
- **`DATABASE_URL` в `.env` — адрес для запуска с ХОСТА (`localhost:5432`), в compose он не
  подставляется.** В `docker-compose.yml` хост базы зашит как `db`. Пока `.env` был не виден
  compose, это держалось само собой; со ссылкой (грабля выше) значение из файла едва не уехало
  в контейнеры — `migrate` падал на `connection to 127.0.0.1:5432 refused`. Не «упрощать»
  обратно в `${DATABASE_URL:-…}`.
- **`WEB_BIND` значит два разных адреса.** В `.env` это адрес **на хосте** (строка `ports`),
  а `lab.web.bind_address` читает ту же переменную и решает, что слушать **внутри**
  контейнера. Совпадение смыслов ломало веб: приложение садилось на loopback контейнера,
  куда docker-proxy не ходит, и опубликованный порт отвечал отказом при `healthy` сервисе
  (heartbeat живёт независимо от сокета). Поэтому в compose сервису `web` жёстко задан
  `WEB_BIND: 0.0.0.0:8080`. Наружу это ничего не открывает: публикуется только
  `127.0.0.1:<порт>` хоста.
- **`docs/` — часть рантайма, а не только документация.** `lab.strategies.presets.cards`
  читает карточки из `docs/research/strategies/` при импорте реестра, поэтому каталог
  обязан быть в образе (`COPY docs ./docs`). Без него в контейнере падает всё, что собирает
  стратегии — `measure`, worker, discovery — с `FileNotFoundError`, при этом тесты
  из репозитория проходят: там `docs/` на месте. Переносить карточки в `config/` или
  в пакет — можно, но тогда править `CARDS_DIR` и путь в 31 карточке.
- **Замер на минутных свечах за длинное окно не влезает в память.** `measure run` на 1m
  за 400 суток (полмиллиона баров) занимает ~2.1 ГБ и получает SIGKILL: команда завершается
  **молча**, код возврата 137, в базе ни строки. Пустой вывод `measure run` — это почти
  всегда OOM, а не «нечего считать»; проверять `dmesg -T | grep -i "killed process"`.
  Пока не переписан на потоковый расчёт: минутные стратегии мерить окном 30–60 суток
  (`--days 45`), длинные окна — на 1h и выше. Контейнерам в compose выставлен `mem_limit`,
  чтобы промах убивал только лабораторию: без него жертву выбирает ядро по всей машине,
  а рядом живут `crypto-trader`, `wake` и `smart-money`.
- **Инструменты в карточках пресетов должны называться по-ccxt.** `BTCUSDT-PERP` из
  карточек `cex-perp-*` фид не понимает: `bybit does not have market symbol BTCUSDT-PERP`,
  замер уходит в `incomplete`. Правильно — `BTC/USDT:USDT` (перп) и `BTC/USDT` (спот).
- **Два «registry» и два «ladder».** `strategies.registry` ≠ `core.registry`;
  `core.ladder` (ступени) ≠ `strategies.meme.ladder` (лесенка продаж). Смотри импорт.

## Что не доделано

- **Живая торговля не проверялась ни на одной площадке.** Всё исполнение прогонялось
  в `paper` и на фейковых транспортах; ни один реальный ордер не отправлялся.
- **Подпись live-транзакций в сетях и на Polymarket не написана** — см. грабли выше.
  Работает только чтение и бумага.
- **Разборщики ончейн-ответов написаны по документации**, а не по живым ответам:
  Helius SWAP, Etherscan V2, tonapi, Hyperliquid `userFills`, Polymarket Data API,
  лидерборд HL (неофициальный хост). Первый живой запуск, скорее всего, потребует
  правок в разборе.
- **Индикаторы Pifagor и Coinmetrika — версии v0 по публичным описаниям.** Pine-кода нет
  ни у одного (профиль TradingView `Pifagor_trade` — 0 скриптов; Coinmetrika — закрытая
  подписка). Закодировано только то, у чего явная база (MFI, SMA, RSI-4w и три «старших»
  индикатора); остальные — заглушки `[ИНДИКАТОР — нужен скрипт]`. Эталон для v1 — даты
  сигналов со скриншотов пользователя на BTC 1W/1M, ряд с 2012 (Bitstamp), не Binance.
- **Индекс внимания NFT: `mentions_growth` без источника упоминаний.** `MentionsSource` —
  протокол, реализации нет; веса индекса — гипотеза и сами предмет замера (`hypothesis=True`).
- **`ops.portfolio.LivePortfolio` на живых балансах не гонялся** — цифры собираются из
  исполнителей и журнала, проверено только на фейках.
- `events()` у всех фидов — опрос REST, не WebSocket. Отдельной таблицы фандинга нет:
  платежи ложатся в журнал через `Costs(funding=...)` (`ops.funding`).
- Ветка `rh`: акции исполнению не подлежат вообще — `SignalOnly`, ступень `signal`
  навсегда, исполняет оператор руками и отмечает кнопкой в боте.

## Как здесь работает Autopilot

Сборка ведётся навыком `/autopilot`. Требования, спецификация и таски — в `.autopilot/`.
Прогресс — `.autopilot/dashboard.html`. Правило: требование из `manifest.md`
может снять только пользователь.

Если работа продолжается — скажи «продолжи автопилот»: состояние поднимется
из `.autopilot/state.js`, переспрашивать ничего не нужно.

<!-- autopilot:end -->
