---
id: nft-early-secondary-floor-v1
branch: nft
source_kind: spec
source_ref: https://docs.magiceden.io/reference/solana-overview
can_backtest: false
timeframe: event
instruments: "коллекции Magic Eden в первые 30 мин после открытия вторички"
venue: magiceden
regime: high_vol
status: hypothesis
params:
  watch_window_min: 30
  min_mint_sellout_pct: 90
  min_sales_per_min: 3
  min_volume_5min_sol: 20
  max_listed_pct_of_supply: 8
  buy_below_floor_pct: 0           # покупать по флору (0) или ниже
  max_price_sol: 1.0
  max_items: 2
  sell_ladder: [{mult: 1.5, pct: 50}, {mult: 2.5, pct: 50}]
  illiquid_after_h: 48
  illiquid_reprice_pct: -10
  decision_latency_target_ms: 2000
---

# Ранняя вторичка: покупка по флору в первые минуты листинга (R24.1)

## Идея
После sold-out минта первые минуты вторички — максимум внимания и минимум предложения; при высокой скорости продаж флор растёт. Покупаем по флору по правилу (скорость продаж, объём, доля листингов), продаём лестницей (G06). Только форвард (R24).

## Правила (кодируемые)

**Вход.** Коллекция из launchpad (`/v2/launchpad/collections`) с `launchDate` прошедшим и минтом ≥ `min_mint_sellout_pct` %. В окне `watch_window_min`: `activities` за 5 мин — продаж/мин ≥ `min_sales_per_min`, объём ≥ `min_volume_5min_sol`, листингов/supply ≤ `max_listed_pct_of_supply` %, флор ≤ `max_price_sol` → купить самый дешёвый листинг (≤ флор·(1 − buy_below_floor_pct/100)).

**Выход.** Листинги на продажу по `sell_ladder` (флор-входа × mult) сразу после покупки; через `illiquid_after_h` часов без продажи — переставить цену на `illiquid_reprice_pct` % (флаг неликвида, R18.2), повторять каждые 24 ч до продажи; жёсткий выход — продажа в лучший бид при флоре < 50 % цены входа.

**Размер.** ≤ `max_items` предметов на коллекцию, ≤ `max_price_sol` за штуку; лимит ветки из `config/limits.yaml`.

**Таймфрейм и инструменты.** События; задержка решения ≤ `decision_latency_target_ms` (R24.2) — измеряется.

**Издержки.** Комиссия ME 2 %, роялти коллекции (по умолчанию 5 %), газ — `config/costs.yaml`.

## Режим рынка
NFT-бум на Solana; в тихом рынке — почти всё неликвид.

## Источники
- [Magic Eden API: Solana overview](https://docs.magiceden.io/reference/solana-overview) — collections, activities, listings, launchpad (проверено 2026-09-05).
- Спец. истории 75, 75a, 78, 80 (R24.1, R24.2, G06, R18.2) — правила лестницы и неликвида.

## Чего не хватает для точного кодирования
- Пороги скорости/объёма — выбор тикета 13 (публичных формализованных правил «ранней вторички» не найдено); сетка форварда по `min_sales_per_min ∈ {1, 3, 6}`.
