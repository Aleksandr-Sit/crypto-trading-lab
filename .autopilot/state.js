window.STATE =
{
  "slug": "crypto-trading-lab",
  "dir": "2026-09-05-crypto-trading-lab--wip",
  "title": "Лаборатория торговых стратегий: крипта, мемы, NFT",
  "mode": "interview",
  "depth": "deep",
  "polish": null,
  "tier": "T3",
  "briefFile": "2026-09-05-brief.md",
  "memoryFile": "CLAUDE.md",
  "skillDir": "/root/.claude/skills/autopilot",
  "startedAt": "2026-09-05T15:32:35+04:00",
  "updatedAt": "2026-09-06T21:15:15+04:00",
  "finishedAt": null,
  "stages": [
    {
      "id": "preflight",
      "status": "done",
      "startedAt": "2026-09-05T15:32:35+04:00",
      "finishedAt": "2026-09-05T15:38:10+04:00"
    },
    {
      "id": "manifest",
      "status": "done",
      "startedAt": "2026-09-05T15:38:10+04:00",
      "finishedAt": "2026-09-05T15:35:24+04:00"
    },
    {
      "id": "briefing",
      "status": "done",
      "startedAt": "2026-09-05T15:35:24+04:00",
      "finishedAt": "2026-09-05T22:05:51+04:00",
      "note": "25 вопросов"
    },
    {
      "id": "spec",
      "status": "done",
      "startedAt": "2026-09-05T22:05:51+04:00",
      "finishedAt": "2026-09-05T22:25:40+04:00"
    },
    {
      "id": "plan",
      "status": "done",
      "startedAt": "2026-09-05T22:25:40+04:00",
      "note": "14 тасков, ярус T3, 6 волн",
      "finishedAt": "2026-09-05T22:32:43+04:00"
    },
    {
      "id": "build",
      "status": "active",
      "startedAt": "2026-09-05T22:32:43+04:00",
      "note": "5 из 14 готовы · 04 в ремонте, 05/06 на ревью, 07 в полёте"
    },
    {
      "id": "review",
      "status": "active",
      "startedAt": "2026-09-05T22:45:47+04:00",
      "note": "проверено 5 из 14"
    },
    {
      "id": "final",
      "status": "pending"
    }
  ],
  "requirements": {
    "total": 45,
    "done": 22,
    "inTicket": 22,
    "inSpec": 0,
    "placeholder": 0,
    "deferred": 1,
    "dropped": 0
  },
  "tickets": [
    {
      "id": "01",
      "title": "Каркас: репозиторий, схема, контракты, конфиги",
      "requirements": [
        "R01",
        "R19",
        "R28i",
        "R29i",
        "R32i"
      ],
      "blockedBy": [],
      "wave": 1,
      "zone": [
        "pyproject.toml",
        "deploy/",
        "src/lab/contracts/",
        "src/lab/core/registry.py",
        "migrations/",
        "config/"
      ],
      "status": "done",
      "retries": 0,
      "repairs": 1,
      "handoffs": 0,
      "startedAt": "2026-09-05T22:33:12+04:00",
      "repairFindings": [
        "CLI не читает DATABASE_URL из .env; README без пути для compose — R01"
      ],
      "finishedAt": "2026-09-05T22:56:01+04:00",
      "tests": {
        "passed": 38,
        "failed": 0
      },
      "commit": "ff1ebac"
    },
    {
      "id": "02",
      "title": "Измерение: издержки, журнал, метрики, порог, симулятор",
      "requirements": [
        "R11",
        "R12",
        "R31i",
        "R34i",
        "R10",
        "A03",
        "A04"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "src/lab/core/costs.py",
        "src/lab/core/journal.py",
        "src/lab/core/measure/",
        "src/lab/data/"
      ],
      "status": "done",
      "retries": 0,
      "repairs": 1,
      "handoffs": 0,
      "startedAt": "2026-09-05T22:56:01+04:00",
      "finishedAt": "2026-09-06T10:43:38+04:00",
      "tests": {
        "passed": 103,
        "failed": 0
      },
      "commit": "5a9b2a5",
      "repairFindings": [
        "ветка из strategy_id ломалась на cex-spot/cex-perp/dex-perp — R11"
      ]
    },
    {
      "id": "03",
      "title": "Риск-ядро и лестница доверия",
      "requirements": [
        "R02",
        "R03",
        "R04",
        "G04",
        "R30i",
        "R20"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "src/lab/core/risk/",
        "src/lab/core/ladder/"
      ],
      "status": "done",
      "retries": 0,
      "repairs": 1,
      "handoffs": 0,
      "startedAt": "2026-09-05T22:56:01+04:00",
      "finishedAt": "2026-09-06T20:52:33+04:00",
      "tests": {
        "passed": 108,
        "failed": 0
      },
      "commit": "098c4ae",
      "repairFindings": [
        "reduce_only блокировался стопами/degraded — позицию нельзя закрыть (G04, История 10)"
      ]
    },
    {
      "id": "13",
      "title": "Каталоги: источники, стратегии, индикаторы, кандидаты",
      "requirements": [
        "R25",
        "R06",
        "G08",
        "R05",
        "R07"
      ],
      "blockedBy": [
        "01"
      ],
      "wave": 2,
      "zone": [
        "docs/research/",
        "candidates/",
        "config/authors.yaml"
      ],
      "status": "done",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0,
      "startedAt": "2026-09-05T22:56:01+04:00",
      "commit": "a11cfa5",
      "finishedAt": "2026-09-06T20:52:33+04:00"
    },
    {
      "id": "04",
      "title": "CEX и Hyperliquid: фиды, исполнители, бэкфилл, сверка",
      "requirements": [
        "R16",
        "R21",
        "R20",
        "R05",
        "R09",
        "R29i"
      ],
      "blockedBy": [
        "02",
        "03"
      ],
      "wave": 3,
      "zone": [
        "src/lab/feeds/cex/",
        "src/lab/executors/cex/",
        "src/lab/data/backfill_cex.py"
      ],
      "status": "repair",
      "retries": 1,
      "repairs": 1,
      "handoffs": 0,
      "startedAt": "2026-09-06T20:52:33+04:00",
      "repairFindings": [
        "paper/live смешаны в positions/balance; при NetworkError отдаётся бумажный баланс; подмена транспорта по PYTEST_CURRENT_TEST"
      ]
    },
    {
      "id": "05",
      "title": "Планировщик и Telegram-бот Trader",
      "requirements": [
        "R13",
        "R02",
        "R03",
        "R04",
        "G04"
      ],
      "blockedBy": [
        "02",
        "03"
      ],
      "wave": 3,
      "zone": [
        "src/lab/bot/",
        "src/lab/ops/scheduler.py",
        "src/lab/ops/outbox.py"
      ],
      "status": "review",
      "retries": 1,
      "repairs": 0,
      "handoffs": 0,
      "startedAt": "2026-09-06T20:52:33+04:00"
    },
    {
      "id": "06",
      "title": "Веб-экран исследователя",
      "requirements": [
        "R13",
        "R34i",
        "R12",
        "A02",
        "A03"
      ],
      "blockedBy": [
        "02",
        "03"
      ],
      "wave": 3,
      "zone": [
        "src/lab/web/"
      ],
      "status": "review",
      "retries": 1,
      "repairs": 0,
      "handoffs": 0,
      "startedAt": "2026-09-06T20:52:33+04:00"
    },
    {
      "id": "07",
      "title": "Стратегии, индикаторы, пресеты, внешние сигналы, акции",
      "requirements": [
        "R06",
        "R07",
        "G08",
        "R01",
        "G01"
      ],
      "blockedBy": [
        "02"
      ],
      "wave": 3,
      "zone": [
        "src/lab/strategies/base.py",
        "src/lab/strategies/presets/",
        "src/lab/strategies/indicators/",
        "src/lab/strategies/external/",
        "src/lab/strategies/stocks/",
        "src/lab/feeds/stocks/",
        "src/lab/feeds/social/"
      ],
      "status": "in-progress",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0,
      "startedAt": "2026-09-06T21:15:15+04:00"
    },
    {
      "id": "08",
      "title": "Ончейн-клиенты, отбор кошельков и копитрейдинг",
      "requirements": [
        "R22",
        "R08",
        "G07"
      ],
      "blockedBy": [
        "04",
        "07"
      ],
      "wave": 4,
      "zone": [
        "src/lab/feeds/chains/",
        "src/lab/wallets/",
        "src/lab/strategies/copy/",
        "src/lab/executors/cex/copy_exchange.py"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "11",
      "title": "Polymarket и Robinhood",
      "requirements": [
        "G02",
        "G01"
      ],
      "blockedBy": [
        "04",
        "07"
      ],
      "wave": 4,
      "zone": [
        "src/lab/feeds/polymarket/",
        "src/lab/executors/polymarket/",
        "src/lab/strategies/prediction/",
        "src/lab/feeds/robinhood/",
        "src/lab/executors/robinhood/"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "09",
      "title": "Мем-коины и ранние стадии: потоки, честность, DEX",
      "requirements": [
        "R17",
        "R24",
        "A05",
        "G07"
      ],
      "blockedBy": [
        "08"
      ],
      "wave": 5,
      "zone": [
        "src/lab/feeds/dex/",
        "src/lab/executors/dex/",
        "src/lab/strategies/meme/"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "10",
      "title": "NFT: трекер, создатели, allowlist, вторичка, минт",
      "requirements": [
        "R18",
        "R23",
        "R24",
        "G03",
        "G06",
        "G09"
      ],
      "blockedBy": [
        "08"
      ],
      "wave": 5,
      "zone": [
        "src/lab/nft/",
        "src/lab/feeds/nft/",
        "src/lab/executors/nft/",
        "src/lab/strategies/nft/"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "12",
      "title": "Поиск кандидатов, переизмерение, перелив, срок годности",
      "requirements": [
        "R15",
        "R14",
        "G05",
        "G10",
        "R10"
      ],
      "blockedBy": [
        "05",
        "08",
        "11"
      ],
      "wave": 5,
      "zone": [
        "src/lab/discovery/",
        "src/lab/ops/jobs/"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    },
    {
      "id": "14",
      "title": "Эксплуатация: квоты, бюджет, доступность, сверка, бэкап, watchdog, деплой",
      "requirements": [
        "R25",
        "R33i",
        "R31i",
        "R28i",
        "R32i",
        "R30i",
        "A01"
      ],
      "blockedBy": [
        "05",
        "12"
      ],
      "wave": 6,
      "zone": [
        "src/lab/ops/feeds_registry.py",
        "src/lab/ops/availability.py",
        "src/lab/ops/watchdog.py",
        "src/lab/ops/backup.py",
        "src/lab/ops/reload.py",
        "src/lab/ops/jobs/reconcile.py",
        "deploy/",
        "scripts/"
      ],
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
    }
  ],
  "singlePass": null,
  "tests": {
    "passed": 103,
    "failed": 0
  },
  "debt": {
    "placeholders": [
      "R29i.1 — проверка прав ключа (withdraw) через API площадки — в тикетах коннекторов"
    ],
    "assumptions": [],
    "emptyEnv": [
      "DATABASE_URL",
      "TELEGRAM_BOT_TOKEN",
      "TELEGRAM_ADMIN_ID",
      "BYBIT_API_KEY",
      "OKX_API_KEY",
      "BINANCE_API_KEY",
      "HYPERLIQUID_PRIVATE_KEY",
      "HELIUS_API_KEY",
      "ALCHEMY_API_KEY",
      "WEB_USER",
      "WEB_PASSWORD"
    ]
  },
  "additions": [],
  "coverage": {
    "findings": 7,
    "missing": 2,
    "half": 5,
    "extra": 9,
    "acted": "2 missing → истории 94a–94b, ветка dex-perp; 5 half → таблица лимитов, R23.2–R23.3, R24.2, G08 v0/v1, G01.2; extra → A05 помечен, личные детали убраны, остальное — craft"
  },
  "concerns": [
    "T01 src/lab/core/registry.py:193 — can_backtest=False всё равно стартует на ступени backtest; начальную ступень должен выставлять ladder (T03)",
    "T01 src/lab/core/registry.py:49 — lab.core.registry.Strategy одноимённа протоколу lab.contracts.Strategy",
    "T01 src/lab/core/registry.py:173 — черновик неполного манифеста сохраняется только если вызывающий сам коммитит",
    "T01 .env.example — BUDGET_MONTH_USD, CHAINS_ENABLED, BACKUP_DIR назначены за будущие таски; Mode дублирует ModeLiteral; StopSpec.max_position_pct и data_keys_report не используются",
    "T01 compose требует -f deploy/docker-compose.yml, спецификация обещала docker compose up -d из корня",
    "T01 config/limits.yaml:12 — real_capital_cap выставлен в 1000 без пометки «впиши своё»",
    "T01 tests/config/test_configs.py:110 — отказ по withdraw=True проверен только на чистой функции, путь старта права не получает",
    "T01 tests/contracts/test_executor_contract.py — cancel пропускается через skip; ассерт OPEN|PARTIAL при требуемом FILLED; venue='fake' захардкожен",
    "T01 tests/core/test_registry.py:61 — черновик читается напрямую из CandidateRow вместо Registry.candidates()",
    "T01 src/lab/cli.py — ветки CLI (пустой реестр, add, дубликат, неполный) без тестов",
    "T01 src/lab/db/engine.py:11 — DEFAULT_URL lab:lab в коде против докстринга; URL продублирован в alembic.ini и conftest",
    "T01 src/lab/executors/fake.py:142 — entry_price при доливке = последний филл, не средневзвешенная",
    "T01 registry.py:171 + config/loader.py:29 — дублирование преобразования ValidationError",
    "T01 .env.example — DB_BIND не описан",
    "T03 [БЕЗОПАСНОСТЬ, чинить первым при возобновлении] src/lab/core/risk/engine.py:107-176 — reduce_only проверяется после стопов: при пробитом стопе закрывающий ордер получает Deny(strategy_stop_*) — позицию нельзя закрыть; условие: reduce_only → Allow до проверок стопов, degraded не блокирует закрытие",
    "T03 engine.py:196-204 — потолок реального капитала считается по марже, при 5× номинал может впятеро превысить $1000 (История 94a)",
    "T03 machine.py:181-210 — promote/demote/breach не проверяют retired/degraded",
    "T03 machine.py:35 — ThresholdFn не совпадает с сигнатурой measure.threshold (kw-only rung), адаптера нет",
    "T03 limits.yaml maintenance_margin_pct=0.5 — величина площадки без пометки «оценка»",
    "T03 machine.py:246 — ttl_s читается из params_json, в манифесте поля нет",
    "T03 engine.py:227 — max_position_pct используется и как доля капитала, и как дистанция до ликвидации",
    "T03 tests — коды unknown_strategy, no_price без тестов; strategy_inactive только для degraded; DbHaltSwitch тест на одной сессии",
    "T03 machine.py:139-147 — evaluate вызывает start(); провал на нижней ступени не пишет переход; demote с backtest молча None",
    "T03 engine.py:243 — available_usd от базы, лимиты от current_usd",
    "T02 runner.py code_version() = unknown в образе без .git; record_fill не проверяет decided_at < ts; тест воспроизводимости сравнивает с кэшем; extra_metrics с опечаткой молча отбрасывается; threshold() читает yaml на каждый вызов; journal reconcile checked считает общие дважды; signal_id = inputs_hash в симуляторе; FIFO-матчер продублирован в journal и simulator",
    "T02 R10: measure_cost/can_backtest не прикреплены к кандидату — очередь (T12) вызывает measure_plan на лету",
    "T13 copy-okx-lead-filtered.md:35, cex-perp-funding-arb-spot-hedge.md:30 — константы в прозе не в params",
    "T13 source_ref карточек Coinmetrika указывает в .autopilot/user-inputs — не попадает в образ",
    "T13 HL-лидеры и смарт-мани кошельки не собраны — долг discovery (T12)",
    "T03 engine.py:116 текст причины для retired «закрытие позиций запрещено» и на открывающий ордер",
    "T04 executor.py:98-108,261-289 — _walk_book и неттинг позиций продублированы (core.costs, FakeExecutor, FakeTransport)",
    "T04 executor.py:306 — _find_on_venue перед каждым live-ордером; InvalidOrder duplicate не обрабатывается",
    "T04 transport.py:29 — max_leverage=50 по умолчанию, когда площадка не сообщила лимит",
    "T04 пустые подклассы BybitExecutor и т.п. с декоративным venue",
    "T04 fills()/positions() с побочными эффектами",
    "T04 backfill: символ без данных → «готово, 0 свечей» с кодом 0",
    "T04 live-пути протестированы только на bybit",
    "T04 transport.py:33 — rate_limit_note без источника",
    "T04 нет funding_history в фиде/бэкфилле — бэктест перпов с фандингом не из чего (долг T14)",
    "T04 paper-филл не сохраняет ref_price для Journal",
    "T04 стартовый баннер venues_report без rights — withdraw-ключ узнаётся только при первом live place (долг T14)",
    "T04 hyperliquid-python-sdk не взят — лидерборд HL остаётся T12"
  ],
  "reviewers": {
    "manifestSpec": "afea90f146a601738",
    "craft": "a992ce8aebc6e8b4b"
  },
  "blind": null
}
