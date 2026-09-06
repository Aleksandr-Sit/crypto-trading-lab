---
# Стартовый список кандидатов (R06.1, R15). Читает discovery (тикет 12).
# Формат: каждый кандидат — отдельный fenced-блок ```yaml с полями
#   kind:       trader | wallet | channel | creator | strategy
#   ref:        уникальный идентификатор на площадке (uniqueCode, адрес, @канал, symbol, id стратегии)
#   venue:      okx | polymarket | hyperliquid | magiceden | telegram | github | manual
#   chain:      polygon | solana | ethereum | base | bnb | ton | arbitrum | null
#   branch:     copy | prediction | nft | meme | cex-spot | cex-perp | dex-perp
#   source_url: откуда взят
#   found_at:   дата сбора (UTC, YYYY-MM-DD)
#   note:       что известно; «не найдено публично» — если ничего
#   priority:   user | auto  (user — назван пользователем, обязателен к разбору)
# Дубликаты discovery определяет по (venue, ref). Отклонённые — в базе, не здесь (R15.2).
format: candidates-seed/v1
collected_at: 2026-09-06
total: 43
---

# Кандидаты — стартовый список

Три имени от пользователя — секция «Названы пользователем». Остальное собрано автоматически из публичных лидербордов **2026-09-06**; цифры — снимок на дату, discovery пересчитает по сделкам (R22). Ничего не выдумано: адреса и коды — только из ответов API.

## Названы пользователем (priority: user)

```yaml
kind: channel
ref: "@pifagortrade"
venue: telegram
chain: null
branch: cex-spot
source_url: https://t.me/s/pifagortrade
found_at: 2026-09-06
priority: user
note: "Подтверждено пользователем 2026-09-06: Pifagor = Дмитрий Енин, https://t.me/pifagortrade — один из его каналов. 86,2 тыс. подписчиков; 19 индикаторов на pifagor.trade, скрипты не публичны. Разбор — docs/research/indicators/pifagor.md."
```

```yaml
kind: channel
ref: "@CoinMetrika"
venue: telegram
chain: null
branch: cex-spot
source_url: https://t.me/CoinMetrika
found_at: 2026-09-06
priority: user
note: "Подтверждено пользователем 2026-09-06: Coinmetrika = Вадим, https://t.me/CoinMetrika — один из его каналов. 36,9 тыс. подписчиков; 8 индикаторов известны по названиям со скриншотов пользователя (user-inputs/). Разбор — docs/research/indicators/coinmetrika.md."
```

```yaml
kind: trader
ref: "CryptosMX"
venue: manual
chain: null
branch: copy
source_url: null
found_at: 2026-09-06
priority: user
note: "Назван пользователем (подтверждено 2026-09-06), но ссылка не дана и публично не найдена: поиск «CryptosMX» по Telegram/X/YouTube/TradingView дал только нерелевантные совпадения (cryptosx.io — биржа security-токенов, не то). Нужна ссылка от пользователя — см. config/authors.yaml [ССЫЛКА — впиши]."
```

## Трейдеры — OKX copy-trading (kind: trader)

Источник: `GET https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20` (без auth), снимок 2026-09-06. Поля: pnl (USDT), pnlRatio, winRatio, aum, copyTraderNum, leadDays. Ответ содержал 20 строк; ниже 9 первых, распарсенных полностью (остальные 11 discovery дочитает сам — тот же запрос).

```yaml
kind: trader
ref: "1499200359BAE11A"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "nick dai***@yinowealth.com; pnl 2 732 508; pnlRatio 0.393; winRatio 0.589; aum 408; copiers 3; leadDays 428. Малый AUM при большом PnL — проверить на накрутку (R22.1)."
```

```yaml
kind: trader
ref: "F6476365DB0D09A3"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "nick maomao12345; pnl 2 429 806; pnlRatio 0.950; winRatio 0.651; aum 7 976; copiers 46; leadDays 1070."
```

```yaml
kind: trader
ref: "811BB1C9681A4FF5"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "nick @guli-1688; pnl 1 478 726; pnlRatio 0.428; winRatio 0.611; aum 557; copiers 6; leadDays 518."
```

```yaml
kind: trader
ref: "811997770117827919"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "nick King_GG; pnl 954 257; pnlRatio 8.666; winRatio 0.554; aum 31 277; copiers 23; leadDays 65. Молодой лидер (65 дней) — флаг «слишком молодой» (R22.1)."
```

```yaml
kind: trader
ref: "08E31CADCFDDCFB8"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "nick Kunpeng Plan; pnl 912 609; pnlRatio 0.122; winRatio 0.578; aum 40 158; copiers 34; leadDays 552."
```

```yaml
kind: trader
ref: "FF48C5939FE6119F"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "nick speculation emperor; pnl 800 375; pnlRatio 0.511; winRatio 0.578; aum 30 859; copiers 21; leadDays 1079."
```

```yaml
kind: trader
ref: "C4A37D4C83CC8F7D"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "nick Prudent-OBV-Basil; pnl 676 863; pnlRatio 0.101; winRatio 0.462; aum 5 000; copiers 1; leadDays 26. Слишком молодой (26 дней) — флаг R22.1."
```

```yaml
kind: trader
ref: "D442CF34E4AEEAF1"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "nick Trader KS; pnl 631 681; pnlRatio 3.690; winRatio 0.622; aum 97 370; copiers 108; leadDays 675. Наибольшее число копирующих в выборке."
```

```yaml
kind: trader
ref: "Oval-dAPI-Pansy"
venue: okx
chain: null
branch: copy
source_url: https://www.okx.com/api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl&limit=20
found_at: 2026-09-06
priority: auto
note: "uniqueCode не распарсен (обрезка ответа) — ref = nickName, discovery должен заменить на uniqueCode; pnl 567 263; pnlRatio 0.201; aum 197 214; copiers 4; leadDays 329."
```

## Кошельки — Polymarket leaderboard (kind: wallet, branch: prediction)

Источник: `GET https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15`, снимок 2026-09-06. `ref` = `proxyWallet` (Polygon). pnl/vol — USD за месяц.

```yaml
kind: wallet
ref: "0xf8831548531d56ad6a4331493243c447a827cd1f"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName Inaccuratestake; rank 1; pnl 3 947 667; vol 19 153 227."
```

```yaml
kind: wallet
ref: "0xb91aeb5accc33a5f9a8615b8ed6b2d352e913987"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName afghj2421; rank 2; pnl 1 404 743; vol 7 699 764."
```

```yaml
kind: wallet
ref: "0xbee54d90051720e27921dc6874f02d646ffca636"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName downtownfee; rank 3; pnl 1 074 595; vol 3 973 706. Высокий pnl/vol — интересен для копирования."
```

```yaml
kind: wallet
ref: "0xfe787d2da716d60e8acff57fb87eb13cd4d10319"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName ferrariChampions2026; rank 4; pnl 692 464; vol 41 661 746. Низкий pnl/vol — вероятно маркет-мейкинг, копировать невыгодно."
```

```yaml
kind: wallet
ref: "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName RN1; rank 5; pnl 512 283; vol 24 635 337."
```

```yaml
kind: wallet
ref: "0x5268527977f700f9bf9b6d5cd843859e4e70135d"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName HomeRunHazard; rank 6; pnl 500 569; vol 19 617 561."
```

```yaml
kind: wallet
ref: "0xe5b70fd855af9258d9463992e4f1ed7987905ee3"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName alwaysfade; rank 7; pnl 489 468; vol 2 506 659. Высокий pnl/vol."
```

```yaml
kind: wallet
ref: "0xe16d3f2a5807999b358affd9445c3a09e45e5e30"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName — адрес-автоген; rank 8; pnl 479 386; vol 4 988 225."
```

```yaml
kind: wallet
ref: "0x52911b9d1d9da4d4783fb3280c0ccd6e73f0d4b6"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName — адрес-автоген; rank 9; pnl 464 614; vol 2 119 139."
```

```yaml
kind: wallet
ref: "0xad9c94a65d1f053b8bb31815865a0d4c64b69889"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName jalenbrunson-official; rank 10; pnl 460 375; vol 2 299 126."
```

```yaml
kind: wallet
ref: "0xd218e474776403a330142299f7796e8ba32eb5c9"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName cigarettes; rank 11; pnl 415 776; vol 6 866 577."
```

```yaml
kind: wallet
ref: "0xb687f00464e33934f5d591f224e71c3559ecaee5"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName alwayslatetotheparty; rank 12; pnl 410 265; vol 81 683 628. Очень низкий pnl/vol — маркет-мейкер."
```

```yaml
kind: wallet
ref: "0x3d6ac5150675a83e2090234414222e968888c2b5"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName brightshieldd; rank 13; pnl 398 415; vol 5 853 453."
```

```yaml
kind: wallet
ref: "0x204f72f35326db932158cba6adff0b9a1da95e14"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName swisstony; rank 14; pnl 389 832; vol 30 278 779."
```

```yaml
kind: wallet
ref: "0x72d815133f9f8b6529e911cf3be492846ce05213"
venue: polymarket
chain: polygon
branch: prediction
source_url: https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15
found_at: 2026-09-06
priority: auto
note: "userName Vatrer; rank 15; pnl 376 618; vol 2 106 650."
```

## Кошельки — Hyperliquid и смарт-мани (kind: wallet)

**Не собрано в этом проходе.** Лидерборд `stats-data.hyperliquid.xyz/Mainnet/leaderboard` — >30 МБ JSON, из среды сборки хост закрыт прокси (см. `docs/research/data-sources.md` §2); публичные подборки смарт-мани кошельков (Nansen, GMGN, Cielo) — витрины без открытых списков адресов, а адреса без источника в этот файл не вносятся. Discovery (тикет 12) собирает их сам: HL — стримом с фильтром `accountValue ≥ 100 000`, Solana/EVM — из Cielo `/feed` и Dune (запросы сообщества). Ссылки на методики: [Nansen: как трекать Solana-кошельки](https://nansen.ai/post/how-to-track-solana-wallets-complete-guide-for-smart-money-analysis), [GMGN blog](https://gmgn.ai/blog/how-to-track-copy-solana-smart-money/).

## NFT-создатели / коллекции — Magic Eden launchpad (kind: creator)

Источник: `GET https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15`, снимок 2026-09-06. В ответе нет полей creator/twitter — `ref` = `symbol` коллекции; создателя discovery выясняет по `creators` минта (Helius DAS). Это входные данные для рейтинга создателей (R23): флор через 1/7/30 дней после минта пересчитывается по activities.

```yaml
kind: creator
ref: "open_wallet_founders_pass"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Open Wallet: Founder's Pass; launch 2026-02-20; size 495; price $1.50. Создатель не указан в API."
```

```yaml
kind: creator
ref: "degenphone"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Degenphone; launch 2026-02-05; size 4 444; price $1.25. Тот же бренд — degenphone_open_edition (2026-01-22, open edition, free): два минта одного создателя — пример для R23."
```

```yaml
kind: creator
ref: "meta_racing_pilots"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Meta Racing Pilots; launch 2026-02-04; size 1 500; price $0.25."
```

```yaml
kind: creator
ref: "ted_trading_club"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Ted Trading Club; launch 2026-01-13; size 1 500; price $2.50."
```

```yaml
kind: creator
ref: "hyperliquid_edition_degn"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Hyperliquid Edition DEGN; launch 2025-12-22; size 4 400; price $0.16."
```

```yaml
kind: creator
ref: "d1srupt0rs"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "D1srupt0rs; launch 2025-12-18; size 3 333; price $0.09."
```

```yaml
kind: creator
ref: "infinite"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Infinite; launch 2025-12-16; size 333; price $0.33."
```

```yaml
kind: creator
ref: "rubicon_gems"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Rubicon Gems; launch 2025-12-09; size 8 888; free mint."
```

```yaml
kind: creator
ref: "caroots"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Caroots; launch 2025-12-09; size 5 000; price $0.15."
```

```yaml
kind: creator
ref: "doopies"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Doopies; launch 2025-12-09; size 8 088; price $1.00."
```

```yaml
kind: creator
ref: "thatgoblin"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "ThatGoblins; launch 2025-12-05; size 3 333; price $0.25."
```

```yaml
kind: creator
ref: "monochams"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Monochams; launch 2025-12-05; size 2 222; price $250.00 — дорогой минт, отдельный кластер для R23."
```

```yaml
kind: creator
ref: "turbo"
venue: magiceden
chain: solana
branch: nft
source_url: https://api-mainnet.magiceden.dev/v2/launchpad/collections?offset=0&limit=15
found_at: 2026-09-06
priority: auto
note: "Turbo; launch 2025-12-05; size 3 333; price $1 337.00 — экстремальная цена, проверить на аномалию данных."
```

## Стратегии — публичные репозитории (kind: strategy)

```yaml
kind: strategy
ref: "freqtrade/freqtrade-strategies"
venue: github
chain: null
branch: cex-spot
source_url: https://github.com/freqtrade/freqtrade-strategies
found_at: 2026-09-06
priority: auto
note: "Официальный сборник стратегий freqtrade (Python, правила входа/выхода в коде). Discovery еженедельно проверяет новые файлы в user_data/strategies."
```

```yaml
kind: strategy
ref: "hummingbot/hummingbot"
venue: github
chain: null
branch: cex-spot
source_url: https://github.com/hummingbot/hummingbot/tree/master/hummingbot/strategy
found_at: 2026-09-06
priority: auto
note: "Маркет-мейкинг и арбитражные стратегии (pure MM, XEMM, arbitrage). Кандидаты для ветки cex-spot с учётом издержек maker."
```

```yaml
kind: strategy
ref: "kernc/backtesting.py"
venue: github
chain: null
branch: cex-spot
source_url: https://kernc.github.io/backtesting.py/doc/examples/
found_at: 2026-09-06
priority: auto
note: "Примеры (SMA cross, оптимизация параметров) — эталон для проверки бэктестера тикета 02."
```
