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
  "updatedAt": "2026-09-05T23:21:28+04:00",
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
      "note": "1 из 14 тасков готов"
    },
    {
      "id": "review",
      "status": "active",
      "startedAt": "2026-09-05T22:45:47+04:00",
      "note": "проверено 1 из 14"
    },
    {
      "id": "final",
      "status": "pending"
    }
  ],
  "requirements": {
    "total": 45,
    "done": 7,
    "inTicket": 37,
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
      "status": "review",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0,
      "startedAt": "2026-09-05T22:56:01+04:00"
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
      "status": "review",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0,
      "startedAt": "2026-09-05T22:56:01+04:00"
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
      "status": "in-progress",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0,
      "startedAt": "2026-09-05T22:56:01+04:00"
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
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
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
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
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
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
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
      "status": "pending",
      "retries": 0,
      "repairs": 0,
      "handoffs": 0
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
    "passed": 38,
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
    "T01 .env.example — DB_BIND не описан"
  ],
  "reviewers": {
    "manifestSpec": "afea90f146a601738",
    "craft": "a992ce8aebc6e8b4b"
  },
  "blind": null
}
