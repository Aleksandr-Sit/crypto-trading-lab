# Каталог источников данных по веткам

> Расширяет `.autopilot/2026-09-05-crypto-trading-lab--wip/research-sources.md` (справочник, проверенный 2026-09-05). Здесь — разбивка по веткам проекта с колонками, которые нужны тикетам 04/08/09/10/11 и дашборду (история 29, R25). Пометка **(?)** — не удалось проверить на текущей странице; **(2026-09-05)** — проверено при сборке этого файла.
>
> Правило: при исчерпании квоты источник не роняет процесс — пропуск тика, фолбэк из колонки «Фолбэк», замер помечается `incomplete` (R25.2).

## Сводка по веткам

| Ветка | Основной источник | Фолбэк | Хватает ли бесплатно на фазу 1 |
|---|---|---|---|
| `cex-spot`, `cex-perp` | Binance/Bybit/OKX публичные REST + архивы data.binance.vision | ccxt между биржами | да |
| `dex-perp` (Hyperliquid) | Info API `POST /info` | Dune / Bitquery для глубокой истории | да для live, нет для истории >5000 свечей |
| `copy` | OKX public copy-trading, Polymarket Data API, HL info API, Cielo feed | ручной список, Dune | частично (лидерборды Bybit/Binance — только веб) |
| `meme` | PumpPortal WS, Helius, DexScreener, Jupiter, STON.fi | Bitquery ($39), Birdeye ($199) | да для потока новых токенов; OHLCV мемов — платно |
| `nft` | Magic Eden, OpenSea v2, Zora, Alchemy NFT API | Tensor по заявке; Blur только on-chain | да, с 7-дневной ротацией ключей OpenSea |
| `prediction` | Polymarket Gamma/CLOB/Data API | — | да |
| `rh` | Robinhood Crypto API; акции — yfinance / Alpha Vantage | — | KYC US-only, вне США не проверено |

---

## 1. `cex-spot` / `cex-perp`

| Источник | Бесплатно / платно | Лимит | Покрытие | История | Фолбэк | Ссылка |
|---|---|---|---|---|---|---|
| Binance REST (spot/futures) | бесплатно без ключа | спот 6000 weight/мин; фьючерсы 2400/мин (?) | свечи, сделки, стакан, фандинг, OI | свечи 1m с 2017 (спот), 2019 (фьючерсы) | Bybit/OKX через ccxt | [Docs](https://developers.binance.com/docs/binance-spot-api-docs/rest-api) |
| data.binance.vision | бесплатно | нет лимита (статика) | kline, aggTrades, trades, fundingRate, metrics по дням/месяцам | полные архивы | — | [GitHub](https://github.com/binance/binance-public-data) |
| Bybit v5 | бесплатно без ключа | 600 req/5 с на IP | kline, сделки, фандинг, OI, стакан | kline с 2020 | Binance | [Rate limit](https://bybit-exchange.github.io/docs/v5/rate-limit) |
| OKX v5 | бесплатно без ключа | candles 40 req/2 с | свечи, история свечей, фандинг | history-candles ограничен (?) | Binance | [Docs](https://www.okx.com/docs-v5/en/) |
| ccxt 4.5.x | бесплатно | лимиты биржи | единый интерфейс, Hyperliquid certified | зависит от биржи | — | [ccxt](https://github.com/ccxt/ccxt) |
| Binance Futures Leaderboard | веб, официального API нет | — | публичные позиции лидеров | — | OKX copy-trading | не найдено публично (2026-09-05) |

**При исчерпании:** переключить `Feed` на ccxt-обёртку другой биржи; бэкфилл — только из архивов data.binance.vision (не бьёт квоту).

## 2. `dex-perp` — Hyperliquid

| Источник | Бесплатно / платно | Лимит | Покрытие | История | Фолбэк | Ссылка |
|---|---|---|---|---|---|---|
| Info API `POST /info` | бесплатно, без ключа/KYC | 1200 weight/мин на IP; WS 10 соединений, 1000 подписок | meta, свечи, стакан, филлы по адресу, фандинг, лидерборд-статы по адресу | `candleSnapshot` — 5000 последних свечей; `userFillsByTime` — 10 000 последних филлов | Dune / Bitquery / Nansen | [Rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits) |
| Лидерборд `stats-data.hyperliquid.xyz/Mainnet/leaderboard` | бесплатно, неофициальный | нет документации | все адреса, `accountValue`, `windowPerformances` (day/week/month/allTime) | снимок | ручной список | community-эндпоинт; **ответ >30 МБ JSON** (2026-09-05: WebFetch отказал по размеру, из среды сборки хост закрыт прокси) — тикет 12 качает стримом и фильтрует по `accountValue` |
| hyperliquid-python-sdk 0.24 | бесплатно | — | торговля + info | — | ccxt | [GitHub](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) |
| Фандинг | бесплатно | как Info API | ставка каждый час (`funding_interval_h: 1` в `config/costs.yaml`) | `fundingHistory` (?) | — | [Funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding) |

**При исчерпании:** Info API — единственный live-источник; при 429 — экспоненциальная пауза, свечи докачивать пачками ≤5000.

## 3. `copy` — лидерборды и смарт-мани

| Источник | Бесплатно / платно | Лимит | Покрытие | История | Фолбэк | Ссылка |
|---|---|---|---|---|---|---|
| OKX `public-lead-traders`, `public-weekly-pnl`, `public-stats`, `public-current-subpositions`, `public-subpositions-history` | бесплатно, без auth | per-endpoint (?) | лид-трейдеры SWAP: `uniqueCode`, PnL, winRatio, AUM, число копирующих, инструменты, открытые позиции | недели PnL | — | [Docs](https://www.okx.com/docs-v5/en/#order-book-trading-copy-trading-get-lead-traders-ranks) — **проверено 2026-09-05**: `GET /api/v5/copytrading/public-lead-traders?instType=SWAP&sortType=pnl` отдал 20 строк |
| Polymarket Data API `/v1/leaderboard`, `/positions`, `/trades`, `/activity` | бесплатно | 1000 req/10 с (/trades 200, /positions 150) | proxyWallet, userName, pnl, volume по периодам | closed-positions | — | [Rate limits](https://docs.polymarket.com/quickstart/introduction/rate-limits) — **проверено 2026-09-05**: `?timePeriod=MONTH&orderBy=PNL&limit=20` работает |
| Hyperliquid leaderboard + `userFills` | бесплатно | см. §2 | филлы лидера по адресу | 10 000 филлов | — | см. §2 |
| Bybit copy-trading | ключ мастера; публичного списка мастеров нет | — | только веб-лидерборд | — | OKX | [Copytrade](https://bybit-exchange.github.io/docs/v5/copytrade) |
| Cielo | free 5000 кредитов/мес, только `/feed`; PnL $89/мес | 5000 кред./мес | фид сделок отслеживаемых кошельков (Solana, EVM) | нет | Helius Enhanced Tx | [Cielo](https://cielo.finance) (?) |
| Dune | free 2500 кредитов/мес, API | 2500 кред./мес | любые SQL-запросы; смарт-мани дашборды сообщества | полная | — | [Pricing](https://dune.com/pricing) (?) |
| Nansen / Arkham / DeBank | платно / по заявке / платно | — | смарт-мани метки | — | Dune | см. research-sources.md §9 |

**При исчерпании:** OKX и Polymarket — держат недельный опрос без проблем; Cielo — снизить частоту до 1 раз/час; Dune — только еженедельный запрос в квоте.

## 4. `meme` — Solana / EVM / BNB / TON

| Источник | Бесплатно / платно | Лимит | Покрытие | История | Фолбэк | Ссылка |
|---|---|---|---|---|---|---|
| PumpPortal WS | `subscribeNewToken`, `subscribeMigration` бесплатно; трейд-стримы 0.01 SOL/10k событий; торговля 0.5–1% | — | новые токены pump.fun, миграции, сделки | нет | Helius webhooks | [Data API](https://pumpportal.fun/data-api/real-time/) |
| Helius | 1M кредитов/мес, 10 RPS RPC, 2 RPS DAS/Enhanced; Dev $49 | 10 RPS | RPC, парсинг свопов по кошельку, DAS | по подписи | публичный RPC (без гарантий) | [Plans](https://www.helius.dev/docs/billing/plans) |
| Jupiter | 1 RPS / 60 rpm; Dev $25 / 10 RPS | 1 RPS | цены, свопы, Ultra | нет | Raydium напрямую | [Rate limits](https://developers.jup.ag/docs/portal/rate-limits) |
| DexScreener | бесплатно | 60–300 rpm | пары, цена, объём, ликвидность, FDV на всех сетях | **нет OHLCV/сделок** | Birdeye ($199) | [Reference](https://docs.dexscreener.com/api/reference) |
| Birdeye | free только price/token list, 15 RPS; OHLCV/trades/wallet — Premium $199 | 15 RPS | OHLCV, сделки, кошельки | полная (платно) | Bitquery | [Pricing](https://birdeye.so/data-api/pricing) |
| Bitquery | trial 7 дней; Personal $39 | 1000 points | pump.fun, four.meme, EVM DEX | полная | — | [Pricing](https://bitquery.io/pricing) |
| RugCheck | бесплатно (?) | не документирован (?) | mint/freeze authority, топ-держатели, риски токена Solana | — | своя проверка через RPC (`getAccountInfo` mint) | [API](https://api.rugcheck.xyz/swagger/index.html) (?) |
| GMGN | публичного API нет | — | — | — | — | [Docs](https://docs.gmgn.ai/) |
| Etherscan V2 | free 5 req/с, один ключ на 60+ сетей вкл. Base | 5 req/с | транзакции, логи, токены | полная | Alchemy | [API](https://etherscan.io/apis) |
| Alchemy | 30M CU/мес, 300 CU/s | 300 CU/s | RPC EVM, Token API, NFT API | полная | публичный RPC | [Plans](https://www.alchemy.com/docs/reference/pricing-plans) |
| The Graph | 100k запросов/мес | — | сабграфы Uniswap/PancakeSwap/Aerodrome | полная | RPC-логи | [Pricing](https://thegraph.com/studio-pricing/) |
| STON.fi `api.ston.fi/v1` | бесплатно, без ключа | (?) | пулы, свопы TON | (?) | tonapi | [STON.fi](https://api.ston.fi/) (?) |
| tonapi.io | без ключа 0.25 rps; с ключом выше | 0.25 rps | аккаунты, jetton'ы, события | полная | — | [tonapi](https://tonapi.io/) |
| four.meme | официального API нет | — | — | — | Bitquery/Codex | — |

**При исчерпании:** Helius → снизить до только-критичных запросов (проверка честности), оценку потока делать по данным PumpPortal/DexScreener; Jupiter 1 RPS — кэш цен 5 с; Birdeye/Bitquery — не покупать до появления кандидатов, прошедших форвард (R24: мемы только форвардом, OHLCV-история для бэктеста не нужна).

## 5. `nft`

| Источник | Бесплатно / платно | Лимит | Покрытие | История | Фолбэк | Ссылка |
|---|---|---|---|---|---|---|
| Magic Eden v2 | без ключа 2 QPS / 120 QPM; ключ по форме | 2 QPS | коллекции, activities, листинги, кошельки, **launchpad** | activities с пагинацией | Alchemy NFT API (EVM) / Helius DAS (Solana) | [Docs](https://docs.magiceden.io/reference/solana-overview) — **проверено 2026-09-05**: `GET /v2/launchpad/collections?limit=20` отдаёт symbol, name, launchDate, size, price, description |
| Tensor | REST Alpha, ключ по заявке | — | Solana коллекции, флор, листинги | — | Magic Eden | [Quickstart](https://dev.tensor.trade/reference/quickstart) |
| OpenSea v2 | free-ключ `POST /api/v2/auth/keys` на **7 дней**, 600 req/ч | 600 req/ч | коллекции, events, листинги, офферы (EVM) | events | Alchemy NFT API | [Keys](https://docs.opensea.io/reference/api-keys) |
| Blur | публичного API нет | — | только on-chain (Blur Pool, Blend) | через Alchemy/Etherscan | — | read-only (G03) |
| Zora | Public REST + coins-sdk, ключ бесплатный | (?) | coins, минты | (?) | — | [Docs](https://docs.zora.co/coins/sdk/public-rest-api) |
| Alchemy NFT API | входит в 30M CU | 300 CU/s | владельцы, метаданные, sales (EVM) | полная | Etherscan | [NFT API](https://www.alchemy.com/docs/reference/nft-api-quickstart) (?) |
| Reservoir / SimpleHash | **закрыты** (15.10.2025 / 03.2025) | — | — | — | — | — |
| Календари минтов | парсинг (nftcalendar.io, Kyzzen, ME launchpad) | — | предстоящие минты | — | — | публичных API не найдено |

**При исчерпании:** Magic Eden 2 QPS — приоритет трекеру коллекций в реестре, остальные — раз в час; OpenSea — автопересоздание ключа каждые 6 дней (обязательно в `ops`).

## 6. `prediction` — Polymarket

| Источник | Бесплатно / платно | Лимит | Покрытие | История | Фолбэк | Ссылка |
|---|---|---|---|---|---|---|
| Gamma API | бесплатно | (?) | рынки, события, теги | все рынки | — | [Gamma](https://docs.polymarket.com/developers/gamma-markets-api/overview) (?) |
| CLOB API | чтение без ключа; торговля — ключи L1/L2 | (?) | стакан, `/prices-history` | история цен по токену | — | [CLOB](https://docs.polymarket.com/developers/CLOB/introduction) (?) |
| Data API | бесплатно | 1000 req/10 с | позиции, сделки, активность, лидерборд | closed-positions | — | [Rate limits](https://docs.polymarket.com/quickstart/introduction/rate-limits) |
| py-clob-client | бесплатно | — | торговля | — | — | [GitHub](https://github.com/Polymarket/py-clob-client) |
| Гео-блок торговли | (?) | — | проверка при старте (G02.2) | — | режим «только замер» | — |

## 7. `rh` — Robinhood и акции

| Источник | Бесплатно / платно | Лимит | Покрытие | История | Фолбэк | Ссылка |
|---|---|---|---|---|---|---|
| Robinhood Crypto API | бесплатно для US-клиентов (KYC); вне США не проверено | (?) | market data, портфель, ордера; auth Ed25519 | — | режим сигналов (G01.1) | [Docs](https://docs.robinhood.com/crypto/trading/) |
| yfinance | бесплатно, без ключа, неофициальный | неформальный (блокировки при частых запросах) | дневные/часовые свечи акций | полная дневная | Alpha Vantage | [PyPI](https://pypi.org/project/yfinance/) |
| Alpha Vantage | free 25 req/день (?) | 25/день (?) | дневные свечи, интрадей | полная | yfinance | [Docs](https://www.alphavantage.co/documentation/) |

## 8. Сигналы и соцсети (форвард-журнал авторов, R06.2)

| Источник | Бесплатно / платно | Лимит | Покрытие | Ссылка |
|---|---|---|---|---|
| Telegram Telethon (MTProto, user-аккаунт) | бесплатно | флуд-лимиты Telegram | публичные каналы + история | [Telethon](https://docs.telethon.dev/) |
| Telegram Bot API | бесплатно | — | только каналы, где бот участник | [Bot API](https://core.telegram.org/bots/api) |
| Веб-превью `t.me/s/<канал>` | бесплатно, без auth | неформальный | последние посты публичного канала — фолбэк без Telethon | проверено 2026-09-05 на `t.me/s/CoinMetrika`, 2026-09-06 на `t.me/s/pifagortrade` (86,2 тыс.) и `t.me/s/coinmetrika` (36,9 тыс.) |
| Каталоги Telegram (telemetr.me, tgchannels.org) | бесплатно | неформальный | архив постов канала с датами — фолбэк для истории авторов (R06.2) | проверено 2026-09-06 на `telemetr.me/content/coinmetrika` |
| Каналы авторов | — | — | `config/authors.yaml`: Pifagor и Coinmetrika найдены; CryptosMX — не найдено публично | `docs/research/indicators/` |
| X API | free-тир отменён (02.2026); pay-per-use $0.005/чтение | — | посты | — |
| YouTube Data API v3 | 10 000 units/день (?) | — | список видео канала (заголовки/описания) | [Docs](https://developers.google.com/youtube/v3) |

## 9. Бэктест-фреймворки и коннекторы

См. research-sources.md §11–12: freqtrade 2026.8, vectorbt OSS 1.1.0, backtesting.py 0.6.6, nautilus_trader 1.228, hummingbot 2.16; ccxt 4.5.77, hyperliquid-python-sdk 0.24, solana-py 0.40, web3.py 8.0, py-clob-client. Публичные репозитории стратегий для тикета 12 (GitHub-поиск): [freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies), [hummingbot strategies](https://github.com/hummingbot/hummingbot/tree/master/hummingbot/strategy), [backtesting.py examples](https://kernc.github.io/backtesting.py/doc/examples/).

## 10. Проверки 2026-09-06 (при сборке карточек и кандидатов)

| Что | Результат |
|---|---|
| OKX `public-lead-traders?instType=SWAP&sortType=pnl&limit=20` | работает без ключа; 20 строк, поля `uniqueCode, nickName, pnl, pnlRatio, winRatio, aum, copyTraderNum, leadDays` → 9 в `candidates/seed.md` |
| Polymarket `/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=15` | работает; поля `proxyWallet, userName, pnl, vol, rank` → 15 в `candidates/seed.md` |
| Magic Eden `/v2/launchpad/collections?offset=0&limit=15` | работает без ключа; полей creator/twitter **нет** — создателя брать из метаданных минта (Helius DAS) |
| Hyperliquid `stats-data.hyperliquid.xyz/Mainnet/leaderboard` | не скачан (размер >30 МБ, хост закрыт прокси среды сборки) — кандидаты HL в seed отсутствуют, собирает discovery |
| Сеть из среды сборки | прямой `curl` к okx.com / polymarket.com отклонён прокси (403 CONNECT); WebFetch работает. На VPS ограничений не ожидается, но `ops` при старте проверяет доступность (R33i) |
| 3Commas / Pionex справки | описывают смысл параметров DCA/грид/мартингейл-ботов, но **не дефолтные числа**; страница Pionex US help по Martingale — 404. Числа в карточках — выбор тикета 13 |

## Что делать при исчерпании — общее правило для `ops.feeds_registry`

1. Счётчик квоты на каждый источник (`R25.1`), прогноз исчерпания = остаток / средний темп за 1 ч.
2. Жёлтый — 70 % квоты: неприоритетные задания (discovery, бэкфилл) откладываются.
3. Красный — 90 %: только live-тики стратегий в замере; остальное → фолбэк или пропуск с событием в журнале.
4. Платные тиры не включаются автоматически — только карточка оператору с суммой (В1).
