---
id: cex-spot-bollinger-meanrev
branch: cex-spot
source_kind: book
source_ref: https://www.bollingerbands.com/bollinger-bands
can_backtest: true
timeframe: 1h
instruments: [BTC/USDT, ETH/USDT, SOL/USDT]
venue: bybit
regime: range
status: hypothesis
params:
  bb_period: 20
  bb_std: 2.0
  rsi_period: 14
  rsi_entry_max: 30
  exit_target: middle_band       # middle_band | upper_band
  stop_loss_pct: 3
  max_hold_bars: 48
  position_pct_of_branch: 25
---

# Возврат к средней по полосам Боллинджера

## Идея
Закрытие ниже нижней полосы (20, 2σ) с подтверждением RSI < 30 — перепроданность; цель — средняя линия. Классика Джона Боллинджера (полосы как мера относительных «дорого/дёшево»).

## Правила (кодируемые)

**Вход.** На закрытии 1h: `close < lower_band(bb_period, bb_std)` и `RSI(rsi_period) < rsi_entry_max`. Лимит-покупка по `close` (действует 1 бар), иначе рыночно на открытии следующего.

**Выход.** `close ≥ SMA(bb_period)` (`exit_target: middle_band`) — продать; стоп `entry·(1 − stop_loss_pct/100)`; по времени — через `max_hold_bars` баров рыночно.

**Размер.** `position_pct_of_branch` % капитала ветки; одна позиция на инструмент.

**Таймфрейм и инструменты.** 1h; 3 ликвидные пары.

**Издержки.** Maker на входе (лимит), taker на выходе/стопе.

## Режим рынка
Боковик; ломается на трендовом падении («walk the band»).

## Источники
- [John Bollinger: Bollinger Bands](https://www.bollingerbands.com/bollinger-bands) — определение (SMA20 ± 2σ), правила чтения; книга «Bollinger on Bollinger Bands» (2001).
- Connors & Alvarez, «Short Term Trading Strategies That Work» (2008) — принцип «покупать откат в тренде», RSI-фильтр (см. `cex-spot-rsi2-connors`).

## Чего не хватает для точного кодирования
- Боллинджер не задаёт единый вход/выход — правило собрано тикетом 13 из его определений; параметры полные.
