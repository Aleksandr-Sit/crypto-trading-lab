---
id: cex-perp-trailing-breakout-bot
branch: cex-perp
source_kind: bot_preset
source_ref: https://www.freqtrade.io/en/stable/stoploss/
can_backtest: true
timeframe: 15m
instruments: [BTCUSDT-PERP, ETHUSDT-PERP, SOLUSDT-PERP]
venue: bybit
regime: trend
status: hypothesis
params:
  breakout_lookback: 96          # 96 × 15m = 24 ч
  initial_stop_pct: 2.0
  trailing_stop_pct: 1.5
  trailing_activate_profit_pct: 1.0   # freqtrade trailing_stop_positive_offset
  leverage: 2
  risk_per_trade_pct: 1.0
  max_positions: 2
  direction: both                # long | short | both
---

# Пробой с трейлинг-стопом (пресет «trailing stop» бота)

## Идея
Вход на пробое максимума/минимума за сутки, выход только по трейлинг-стопу (модель freqtrade: `trailing_stop` с `trailing_stop_positive` и `trailing_stop_positive_offset`). Даёт длинные хвосты в тренде.

## Правила (кодируемые)

**Вход.** Long: `close > max(high[−breakout_lookback..−1])`; short (если `direction` допускает): `close < min(low[...])`. Вход рыночно на закрытии свечи 15m. Один вход на инструмент, пока позиция открыта.

**Выход.** Начальный стоп `entry·(1 ∓ initial_stop_pct/100)`. Когда нереализованная прибыль ≥ `trailing_activate_profit_pct`, стоп = `max_price·(1 − trailing_stop_pct/100)` (для лонга; зеркально для шорта), только подтягивается. Тейка нет.

**Размер.** `qty = capital_branch·risk_per_trade_pct/100 / initial_stop_pct%·price` (риск 1 % на сделку), плечо ≤ `leverage`; ≤ `max_positions` одновременно. R20.1: ликвидация дальше стопа.

**Таймфрейм и инструменты.** 15m; 3 самых ликвидных перпа.

**Издержки.** Taker вход/выход (стоп — рыночный), фандинг за время удержания, проскальзывание.

## Режим рынка
Тренд/высокая волатильность; в боковике серия ложных пробоев по −2 %.

## Источники
- [freqtrade docs: Stop Loss / trailing stop](https://www.freqtrade.io/en/stable/stoploss/) — семантика `trailing_stop`, `trailing_stop_positive`, `trailing_stop_positive_offset`, `trailing_only_offset_is_reached`.
- Пробой канала — см. `cex-perp-turtle-donchian` (Turtle System 1) как первоисточник идеи.

## Чего не хватает для точного кодирования
- Конкретный «бот» с таким пресетом на биржах не назван; карточка формализует распространённый паттерн (freqtrade trailing) — тикет 07 кодирует как есть.
