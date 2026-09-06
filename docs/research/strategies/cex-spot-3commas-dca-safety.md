---
id: cex-spot-3commas-dca-safety
branch: cex-spot
source_kind: bot_preset
source_ref: https://help.3commas.io/en/articles/3108940-dca-bot-interface-and-main-settings
can_backtest: true
timeframe: 1m
instruments: [BTC/USDT, ETH/USDT, SOL/USDT]
venue: bybit
regime: range
status: hypothesis
params:
  base_order_usd: 20
  safety_order_usd: 20
  max_safety_orders: 5
  max_active_safety_orders: 2
  price_deviation_pct: 1.5
  safety_volume_scale: 1.5
  safety_step_scale: 1.2
  take_profit_pct: 1.5
  take_profit_base: average      # average | base
  stop_loss_pct: 0               # 0 = выключен (как у 3Commas по умолчанию)
  max_deals: 3
---

# DCA-бот 3Commas (усреднение «safety orders»)

## Идея
Классический пресет DCA-бота 3Commas: базовый ордер, затем страховочные ордера ниже по сетке с растущим шагом и объёмом; сделка закрывается по проценту от средней цены. Эксплуатирует возврат цены к среднему в боковике.

## Правила (кодируемые)

**Вход.** Условие старта сделки — «open new trade immediately» (по умолчанию у 3Commas, [источник](https://help.3commas.io/en/articles/3108940-dca-bot-interface-and-main-settings)): при отсутствии открытой сделки по паре — рыночная покупка на `base_order_usd`. Страховочный ордер `k` (k=1..`max_safety_orders`) — лимит на покупку по цене `entry * (1 - dev_k/100)`, где `dev_k = price_deviation_pct * Σ_{i=0}^{k-1} safety_step_scale^i`; объём `safety_order_usd * safety_volume_scale^(k-1)`. Одновременно в стакане не больше `max_active_safety_orders` страховочных.

**Выход.** Лимит на продажу всего объёма по `avg_price * (1 + take_profit_pct/100)` (`take_profit_base: average`), переставляется после каждого исполненного страховочного. Стоп-лосс — `stop_loss_pct` от `avg_price`, если > 0.

**Размер.** Максимальные вложения в сделку = `base + Σ safety` = 20 + 20·(1+1.5+2.25+3.375+5.06) ≈ 263 USD; `max_deals` одновременных сделок; итого ≤ 3·263 USD — сверяется с лимитом ветки `config/limits.yaml`.

**Таймфрейм и инструменты.** Исполнение по 1m свечам (лимиты считаются по high/low); отбор — топ-3 пары по обороту площадки среди USDT-спота.

**Издержки.** Комиссия maker на страховочных и тейке, taker на базовом; проскальзывание по стакану — `config/costs.yaml`.

## Режим рынка
Работает в боковике и слабом восходящем тренде; ломается в затяжном падении (все страховочные исполнены, позиция «висит» без стопа) — в замер добавить метрику максимального времени в сделке.

## Источники
- [3Commas Help: DCA Bot interface and main settings](https://help.3commas.io/en/articles/3108940-dca-bot-interface-and-main-settings) — смысл параметров (deviation, step scale, volume scale, TP от average/base); дефолт «open new trade immediately».
- [3Commas Help: Averaging order settings](https://help.3commas.io/en/articles/11983699-dca-bot-averaging-order-settings-explained) — формула шага/объёма.

## Чего не хватает для точного кодирования
- Числовые значения по умолчанию в справке 3Commas не приведены (проверено 2026-09-06); значения в `params` — типичные настройки, выбранные тикетом 13 как стартовая гипотеза, не факт из документации.
- Вариант старта по индикатору (RSI/TradingView-сигнал) — отдельная карточка при необходимости.
