---
id: nft-creator-track-record
branch: nft
source_kind: spec
source_ref: https://docs.magiceden.io/reference/solana-overview
can_backtest: true
timeframe: 1d
instruments: "коллекции Magic Eden с известным создателем (creators[] минта)"
venue: magiceden
regime: any
status: hypothesis
params:
  min_prior_collections: 2
  min_floor_7d_over_mint_median: 1.5   # медиана флор(7д)/цена минта по прошлым коллекциям
  min_success_share: 0.6               # доля прошлых коллекций с флор(30д) > цены минта
  buy_within_days: 1                   # покупать на вторичке в первые сутки
  max_price_over_mint_mult: 1.3
  sell_after_days: 7
  max_items: 2
---

# Отбор по рейтингу создателя: покупать первые сутки у авторов с историей (R23)

## Идея
Создатель с несколькими прошлыми коллекциями, чей флор держался выше цены минта, — единственный «фундаментальный» фактор у NFT, проверяемый по данным. Единственная NFT-карточка с `can_backtest: true`: история флора и минтов доступна через activities.

## Правила (кодируемые)

**Вход.** Для новой коллекции определить создателя (`creators[0]` в метаданных минта, Helius DAS); по прошлым коллекциям того же адреса (≥ `min_prior_collections`): медиана `floor(7d)/mint_price ≥ min_floor_7d_over_mint_median`, доля коллекций с `floor(30d) > mint_price ≥ min_success_share` → купить по флору в первые `buy_within_days` суток, если флор ≤ `mint_price·max_price_over_mint_mult`.

**Выход.** Листинг по флору через `sell_after_days` дней (продажа по биду, если листинг не исполнен за 48 ч — правило неликвида R18.2).

**Размер.** ≤ `max_items`; лимит ветки.

**Таймфрейм и инструменты.** 1d агрегаты флора/объёма; бэктест по истории `activities` коллекций 2024–2026.

**Издержки.** Комиссия + роялти + газ.

## Режим рынка
Любой; сигналов мало (создатели с ≥2 коллекциями редки).

## Источники
- [Magic Eden API](https://docs.magiceden.io/reference/solana-overview) — collections, activities (история продаж/листингов).
- Helius DAS (`getAsset` → `creators`) — [Helius docs](https://www.helius.dev/docs) (?).
- Спец. истории 73 (R23), 80 (R18.2).

## Чего не хватает для точного кодирования
- Пороги — выбор тикета 13; связь «коллекция → создатель» для launchpad-коллекций ME требует минта хотя бы одного предмета (метаданные) — discovery заполняет.
