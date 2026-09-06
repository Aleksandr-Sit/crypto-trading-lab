---
id: prediction-pm-longshot-fade
branch: prediction
source_kind: paper
source_ref: https://doi.org/10.1086/655844
can_backtest: true
timeframe: event
instruments: "рынки Polymarket с датой резолюции ≤ 30 дней и объёмом ≥ 100k USD"
venue: polymarket
regime: any
status: hypothesis
params:
  favorite_price_min: 0.90
  favorite_price_max: 0.97
  max_days_to_resolution: 30
  min_market_volume_usd: 100000
  max_spread: 0.02
  size_usd: 10
  max_open_markets: 20
  exclude_tags: [crypto-price]   # рынки «цена BTC выше X» коррелируют с портфелем
---

# Покупка фаворита (fade longshot bias) на Polymarket

## Идея
Favorite-longshot bias: исходы с низкой ценой систематически переоценены, фавориты — недооценены (Snowberg & Wolfers 2010 на ставках). Гипотеза: покупка фаворита по 0,90–0,97 с близкой резолюцией даёт положительное ожидание после спреда. Проверяется Brier-score и доходностью к резолюции.

## Правила (кодируемые)

**Вход.** Ежедневно: рынки Gamma API с `endDate − now ≤ max_days_to_resolution`, `volume ≥ min_market_volume_usd`, тег не в `exclude_tags`; outcome с `best_ask ∈ [favorite_price_min, favorite_price_max]` и `ask − bid ≤ max_spread` → лимит-покупка `size_usd` по `best_ask`.

**Выход.** Держать до резолюции. Досрочно продать, если цена упала ниже `favorite_price_min − 0.10` (стоп) — рыночно.

**Размер.** `size_usd` на рынок, ≤ `max_open_markets`.

**Таймфрейм и инструменты.** События; история — `/prices-history` + резолюции из Gamma (`closed`, `outcomePrices`).

**Издержки.** Спред, газ; комиссий 0.

## Режим рынка
Не зависит от крипты. Ломается при кластеризованных «сюрпризах» (много рынков с общим фактором — выборы, спорт-турнир).

## Источники
- Snowberg, Wolfers (2010), «Explaining the Favorite–Longshot Bias: Is it Risk-Love or Misperceptions?», JPE 118(4) — [DOI](https://doi.org/10.1086/655844).
- [Polymarket Gamma API](https://docs.polymarket.com/developers/gamma-markets-api/overview) — рынки, `endDate`, `volume`, теги.

## Чего не хватает для точного кодирования
- Наличие/сила эффекта на Polymarket — не подтверждено публикацией (проверить самим); диапазон цен — выбор тикета 13.
