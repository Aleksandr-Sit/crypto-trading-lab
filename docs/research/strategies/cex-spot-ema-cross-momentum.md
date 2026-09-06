---
id: cex-spot-ema-cross-momentum
branch: cex-spot
source_kind: github
source_ref: https://github.com/freqtrade/freqtrade-strategies
can_backtest: true
timeframe: 4h
instruments: [BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT]
venue: binance
regime: trend
status: hypothesis
params:
  fast_ema: 20
  slow_ema: 50
  trend_filter_ema: 200
  stop_loss_pct: 5
  trailing_stop_pct: 3
  position_pct_of_branch: 20
  max_positions: 3
---

# Пересечение EMA 20/50 с фильтром EMA 200

## Идея
Простейшая трендследящая: покупать, когда быстрая EMA пересекает медленную снизу вверх выше долгосрочной EMA. Эталонная гипотеза «нулевого уровня» из сборника freqtrade-strategies; нужна как база для сравнения с авторскими индикаторами.

## Правила (кодируемые)

**Вход.** На закрытии свечи 4h: `EMA(fast)[t−1] ≤ EMA(slow)[t−1]` и `EMA(fast)[t] > EMA(slow)[t]` и `close > EMA(trend_filter)`. Рыночная покупка на следующем открытии.

**Выход.** Обратное пересечение (`EMA(fast) < EMA(slow)`), либо стоп `entry·(1 − stop_loss_pct/100)`, либо трейлинг `max_close·(1 − trailing_stop_pct/100)` — что раньше.

**Размер.** `position_pct_of_branch` % капитала ветки на позицию; ≤ `max_positions`.

**Таймфрейм и инструменты.** 4h; топ-5 по обороту USDT-спота.

**Издержки.** Taker вход/выход, проскальзывание.

## Режим рынка
Тренд; в боковике — пила пересечений с частыми −1…−2 %.

## Источники
- [freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies) — семейство EMA/SMA-cross стратегий (файлы `user_data/strategies/*.py`), структура entry/exit сигналов.
- [backtesting.py: Quick Start (SmaCross)](https://kernc.github.io/backtesting.py/doc/examples/Quick%20Start%20User%20Guide.html) — эталонная реализация кроссовера для проверки бэктестера.

## Чего не хватает для точного кодирования
- Ничего; параметры заданы. Сетка замера: fast ∈ {10, 20}, slow ∈ {50, 100}.
