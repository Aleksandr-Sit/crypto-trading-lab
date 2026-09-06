---
id: cex-spot-rsi2-connors
branch: cex-spot
source_kind: book
source_ref: https://www.amazon.com/Short-Term-Trading-Strategies-That-Work/dp/0981923909
can_backtest: true
timeframe: 1d
instruments: [BTC/USDT, ETH/USDT]
venue: binance
regime: trend
status: hypothesis
params:
  trend_sma: 200
  rsi_period: 2
  rsi_entry_max: 10
  exit_sma: 5
  scale_in_levels: 2             # 2-я покупка при RSI2 < 5 на следующий день
  position_pct_of_branch: 20
  max_hold_days: 10
---

# RSI(2) Коннорса: покупка отката в тренде

## Идея
Ларри Коннорс: при цене выше SMA200 покупать, когда 2-периодный RSI падает ниже 10, выходить при закрытии выше SMA5. Короткие сделки с высоким win-rate.

## Правила (кодируемые)

**Вход.** На закрытии 1d: `close > SMA(trend_sma)` и `RSI(rsi_period) < rsi_entry_max` → покупка на закрытии (или открытии следующего дня — параметр исполнения). Если на следующий день `RSI(2) < 5` — вторая покупка того же размера (`scale_in_levels: 2`).

**Выход.** `close > SMA(exit_sma)` → продать всё на закрытии. Стопа в оригинале нет; проектный стоп по времени `max_hold_days` (рыночно) и стоп ветки по риску из `config/limits.yaml`.

**Размер.** `position_pct_of_branch` % на уровень (до 2 уровней = 40 %).

**Таймфрейм и инструменты.** 1d; BTC, ETH.

**Издержки.** Taker; сделок мало, издержки невелики.

## Режим рынка
Бычий тренд с откатами; при пробое SMA200 вниз стратегия выключается фильтром.

## Источники
- Connors, Alvarez, «Short Term Trading Strategies That Work» (TradingMarkets, 2008), гл. RSI(2): правила «200-day MA, RSI(2)<10 buy, exit close > 5-day MA».
- [TradingView: Connors RSI2 — публичные реализации](https://www.tradingview.com/scripts/connorsrsi/) — сверка формулы RSI(2).

## Чего не хватает для точного кодирования
- Оригинал — для акций/ETF; применимость к крипте — предмет замера. Стоп по времени добавлен тикетом 13.
