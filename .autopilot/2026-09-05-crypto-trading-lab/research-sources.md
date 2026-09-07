# Справочник источников данных и API (проверено 2026-09-05)

> Собрано субагентом-исследователем по актуальным страницам документации. Пометка **(?)** = не удалось проверить.
> Это входные факты для спецификации; полный каталог с расширением уходит в `docs/research/` тикетом сборки.

## 1. CEX

| | API / ключ | Бесплатный тир и лимиты | История | Лидерборд |
|---|---|---|---|---|
| **Bybit v5** | Публичные market-эндпоинты без ключа; торговля — ключ. | 600 req / 5 с на IP; per-UID 10–50 rps. | Kline, сделки — бесплатно. | Copy-trading API только для мастера; **публичного списка мастеров нет**, лидерборд — только веб. [Docs](https://bybit-exchange.github.io/docs/v5/rate-limit), [Copytrade](https://bybit-exchange.github.io/docs/v5/copytrade) |
| **OKX v5** | Публичные данные без ключа. | Per-endpoint (candles 40 req/2 с). | Свечи + история. | **Публичные copy-trading эндпоинты без auth**: `public-lead-traders`, `public-weekly-pnl`, `public-stats`, `public-current-subpositions`, `public-subpositions-history`. [Docs](https://www.okx.com/docs-v5/en/) |
| **Binance** | Публичные данные без ключа. | Спот 6000 weight/мин; фьючерсы 2400/мин (?). | **Полные архивы бесплатно** на data.binance.vision. | Futures Leaderboard — официального API нет. [public-data](https://github.com/binance/binance-public-data) |

## 2. Hyperliquid
- Info API `POST /info` — без ключа, без KYC. **1200 weight/мин на IP**. WS: 10 соединений, 1000 подписок.
- История по адресу: `userFills` ≤2000; `userFillsByTime` — **только 10 000 последних филлов**; `candleSnapshot` — **5000 последних свечей**. Глубокая история — Dune/Bitquery/Nansen.
- Лидерборд: в официальных docs отсутствует; сообщество использует `stats-data.hyperliquid.xyz/Mainnet/leaderboard` (?). [Rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits), [hyperliquid-stats](https://github.com/thunderhead-labs/hyperliquid-stats)

## 3. Solana
| Сервис | Free | Примечание |
|---|---|---|
| **Helius** | 1M кредитов/мес, 10 RPS RPC, 2 RPS DAS/Enhanced. Dev $49. | Enhanced Transactions = парсинг свопов по кошельку. [Plans](https://www.helius.dev/docs/billing/plans) |
| **Birdeye** | Free: только price/token list, 15 RPS. **OHLCV/trades/wallet — Premium $199** (Lite $39, Starter $99 ограниченно (?)). | [Pricing](https://birdeye.so/data-api/pricing) |
| **Jupiter** | 1 RPS / 60 rpm; Dev $25/10 RPS. | Price v3, Swap, Ultra. [Rate limits](https://developers.jup.ag/docs/portal/rate-limits) |
| **PumpPortal** | WS `subscribeNewToken`/`subscribeMigration` бесплатно; трейд-стримы 0.01 SOL/10k событий. Торговля 0.5–1%. | [Data API](https://pumpportal.fun/data-api/real-time/) |
| **GMGN** | **Публичного data-API нет**; только Cooperation API по заявке. | [Docs](https://docs.gmgn.ai/) |
| **DexScreener** | 60–300 rpm без ключа. **Нет OHLCV и истории сделок.** | [Reference](https://docs.dexscreener.com/api/reference) |
| **Bitquery** | 7-дневный trial, 1000 points; Personal $39. | pump.fun/four.meme. [Pricing](https://bitquery.io/pricing) |

## 4. EVM
- **Alchemy**: free 30M CU/мес, 300 CU/s, **NFT API и Token API включены**. [Plans](https://www.alchemy.com/docs/reference/pricing-plans)
- **Etherscan V2** (один ключ на 60+ сетей, вкл. Base): free 5 req/с. [API](https://etherscan.io/apis)
- **The Graph**: 100k запросов/мес бесплатно; Uniswap сабграфы. [Pricing](https://thegraph.com/studio-pricing/)

## 5. BNB Chain / TON
- **four.meme**: официального API нет; через Bitquery/Codex. **PancakeSwap**: сабграфы + RPC.
- **tonapi.io**: без ключа 0.25 rps; с ключом выше. **STON.fi** `api.ston.fi/v1` без ключа. DeDust/Getgems — публичных API не найдено (?).

## 6. NFT
| | Статус 2026 |
|---|---|
| **Magic Eden** | Без ключа 2 QPS / 120 QPM; ключ по форме. Collections, activities, wallets, listings, **launchpad**. [Docs](https://docs.magiceden.io/reference/solana-overview) |
| **Tensor** | REST «Alpha», ключ только по заявке. [Quickstart](https://dev.tensor.trade/reference/quickstart) |
| **OpenSea v2** | Free-ключ через `POST /api/v2/auth/keys` (**7 дней**, 600 req/ч). [Keys](https://docs.opensea.io/reference/api-keys) |
| **Blur** | Публичного API нет; данные on-chain/Alchemy. |
| **Reservoir** | **Закрыт 15.10.2025**. **SimpleHash** — закрыт (03.2025). |
| **Zora** | Public REST + coins-sdk, ключ бесплатный. [Docs](https://docs.zora.co/coins/sdk/public-rest-api) |
| Календари минтов | Публичных API не найдено — парсинг (nftcalendar.io, Kyzzen, ME launchpad). |

## 7. Polymarket
- **Gamma**, **CLOB**, **Data API** — чтение без ключа/KYC. Data API 1000 req/10 с (/trades 200, /positions 150).
- Data API: `/positions`, `/closed-positions`, `/trades`, `/activity`, **`/v1/leaderboard`** — публично.
- История цен: CLOB `/prices-history`. Торговля — ключи L1/L2, гео-блок (?). `py-clob-client` актуален. [Rate limits](https://docs.polymarket.com/quickstart/introduction/rate-limits)

## 8. Robinhood Crypto API
Официальный с 05.2024: market data, портфель, ордера; auth — API key + Ed25519. **Только для US-клиентов Robinhood Crypto** (KYC). Доступность вне США — не удалось проверить. [Docs](https://docs.robinhood.com/crypto/trading/)

## 9. Копитрейд / смарт-мани
- **Cielo**: free 5000 кредитов/мес, только `/feed`; PnL — $89/мес. **Nansen**: free нет, $10/10k кредитов. **Arkham**: по заявке. **Dune**: free 2500 кредитов/мес, API есть. **DeBank**: платно.

## 10. Сигналы / соцсети
- **Telegram**: Bot API читает только где бот участник; **Telethon (MTProto, user-аккаунт)** читает публичные каналы и историю.
- **X API**: free-тир отменён (02.2026); pay-per-use $0.005/чтение. **LunarCrush**: $90/мес.

## 11. Бэктест-фреймворки (все активны на 09.2026)
freqtrade 2026.8 · vectorbt OSS 1.1.0 (PRO $25/мес) · backtesting.py 0.6.6 · nautilus_trader 1.228 (адаптеры Binance/Bybit/OKX/Hyperliquid/Polymarket) · hummingbot 2.16

## 12. Коннекторы
ccxt 4.5.77 (Binance, OKX, Bybit, **Hyperliquid certified**) · hyperliquid-python-sdk 0.24 · solana-py 0.40 + solders · web3.py 8.0 · py-clob-client

## Итог: бесплатно для фазы 1 vs платное/закрыто

| Достаточно бесплатно | Упирается в платное / закрыто |
|---|---|
| CEX market data + архивы data.binance.vision | Bybit/Binance лидерборды (только веб/скраперы) |
| **OKX public copy-trading** — единственный CEX с публичным лидербордом | Hyperliquid глубокая история и лидерборд → Nansen/Dune |
| Hyperliquid info API (live + недавняя история) | Birdeye OHLCV/trades/wallet ($199) |
| Helius 1M, Jupiter 1 RPS, DexScreener, PumpPortal new-token стрим | PumpPortal трейд-стримы, Bitquery ($39+), GMGN (нет API) |
| Alchemy 30M CU (+NFT API), Etherscan V2, The Graph, STON.fi, tonapi | tonapi под нагрузкой, DeBank, four.meme |
| Polymarket Gamma/CLOB/Data API + leaderboard | Robinhood (US-only, KYC) |
| Magic Eden 2 QPS, OpenSea 7-дневные ключи, Zora | Tensor (по заявке), Reservoir/SimpleHash закрыты, Blur без API, календари — парсинг |
| Dune 2500, Cielo feed, Telethon | Nansen, Arkham, Cielo PnL, X API, LunarCrush |
