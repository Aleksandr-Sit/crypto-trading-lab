---
id: meme-sol-pumpfun-migration-v1
branch: meme
source_kind: api_docs
source_ref: https://pumpportal.fun/data-api/real-time/
can_backtest: false
timeframe: event
instruments: "токены pump.fun после миграции (событие subscribeMigration)"
venue: jupiter
regime: high_vol
status: hypothesis
params:
  entry_delay_s: 20                 # ждать после миграции, чтобы пропустить первых снайперов
  entry_window_s: 180               # не входить позже 3 мин после миграции
  min_liquidity_usd: 15000
  max_top10_holders_pct: 30
  require_mint_authority_none: true
  require_freeze_authority_none: true
  min_token_age_min: 0
  max_dev_holding_pct: 5
  size_usd: 10
  slippage_bps: 300
  priority_fee_sol: 0.001
  tp1_mult: 2.0
  tp1_sell_pct: 50
  tp2_mult: 4.0
  tp2_sell_pct: 30
  trailing_stop_pct: 40
  hard_stop_pct: 50
  max_hold_min: 240
  max_open: 5
---

# Вход после миграции pump.fun с фильтрами честности и лестницей выхода

## Идея
Миграция бондинг-кривой pump.fun на DEX (PumpSwap/Raydium) — публичное событие с притоком ликвидности и внимания. Входим с задержкой (после первых снайперов), только если токен проходит чек-лист честности (R17.2), выходим лестницей (A05). Только форвард (R24).

## Правила (кодируемые)

**Вход.** Событие `subscribeMigration` (PumpPortal WS) → через `entry_delay_s`, но не позже `entry_window_s`: чек-лист по RPC/RugCheck — `mint_authority == null`, `freeze_authority == null`, доля топ-10 держателей ≤ `max_top10_holders_pct`, доля dev-кошелька (создатель из события) ≤ `max_dev_holding_pct`, ликвидность пула (DexScreener/пул) ≥ `min_liquidity_usd`, mint не в блок-листе. Все — pass → своп через Jupiter на `size_usd` с `slippage_bps`, `priority_fee_sol`. Провал любого — запись в журнал с причиной, без покупки.

**Выход.** Лестница: при `price ≥ entry·tp1_mult` продать `tp1_sell_pct` %; при `≥ entry·tp2_mult` — `tp2_sell_pct` %; остаток — трейлинг от максимума `trailing_stop_pct` %. Жёсткий стоп `−hard_stop_pct` % и стоп по времени `max_hold_min` — продать всё.

**Размер.** `size_usd` на токен; ≤ `max_open` одновременно; неудачные транзакции — в издержки (R17.4).

**Таймфрейм и инструменты.** События; цены — котировки Jupiter каждые 5 с (кэш 1 RPS).

**Издержки.** Fee пула (pumpswap/raydium 25–100 bps по `config/costs.yaml`), priority fee, газ, проскальзывание; неудачные попытки.

## Режим рынка
Активная мем-фаза Solana; в тихом рынке — большинство миграций уходят в ноль, стратегия сгорает по стопам — это и есть замер.

## Источники
- [PumpPortal Data API: real-time](https://pumpportal.fun/data-api/real-time/) — `subscribeNewToken`, `subscribeMigration`, `subscribeTokenTrade`.
- [Jupiter API rate limits](https://developers.jup.ag/docs/portal/rate-limits); [RugCheck API](https://api.rugcheck.xyz/swagger/index.html) (?) — проверка authority/держателей; альтернатива — `getAccountInfo` mint + `getTokenLargestAccounts`.
- Спец. истории 65–71 (R17, R17.2, A05, R24, R17.4) — лестница выхода и правило «только форвард».

## Чего не хватает для точного кодирования
- Пороги честности и лестницы — выбор тикета 13; сетка форварда по `entry_delay_s ∈ {5, 20, 60}`.
- Публичный первоисточник «стратегии миграции» с числами не найден (только общие гайды GMGN/Nansen) — карточка формализована из механики площадки.
