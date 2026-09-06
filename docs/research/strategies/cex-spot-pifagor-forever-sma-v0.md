---
id: cex-spot-pifagor-forever-sma-v0
branch: cex-spot
source_kind: author_indicator
source_ref: https://www.pifagor.trade/indicators.html
can_backtest: true
timeframe: 1d
instruments: [BTC/USDT]
venue: binance
regime: trend
status: hypothesis
params:
  sma_period_days: 200
  sma_variant: daily             # daily (SMA200d) | weekly (SMA200w ≈ 1400d)
  entry_buffer_pct: 1.0
  exit_buffer_pct: 1.0
  position_pct_of_branch: 50
  indicator_version: v0
---

# Pifagor «Forever SMA» — порт v0 (BTC vs долгосрочная SMA)

## Идея
Автор: «Глобальный индикатор для торговли биткоином. Долгосрочный взгляд на тренд» на базе SMA ([источник](https://www.pifagor.trade/indicators.html)). Период и правила не раскрыты; v0 — классический фильтр «цена выше/ниже SMA200» с буфером против пилы.

## Правила (кодируемые)

**Вход.** На закрытии 1d: `close > SMA(sma_period_days)·(1 + entry_buffer_pct/100)` и позиции нет → покупка на открытии следующего дня.

**Выход.** `close < SMA·(1 − exit_buffer_pct/100)` → продать всё.

**Размер.** `position_pct_of_branch` % капитала ветки; одна позиция.

**Таймфрейм и инструменты.** 1d; BTC. Сетка замера: `sma_period_days ∈ {100, 200}`, `sma_variant ∈ {daily, weekly}`.

**Издержки.** Taker; сделок мало.

## Режим рынка
Многомесячные тренды; в широком боковике — 2–4 ложных сигнала в год.

## Источники
- [pifagor.trade/indicators.html](https://www.pifagor.trade/indicators.html) — описание «Forever SMA»; видео `bESbdnP422s` (не разобрано).

## Чего не хватает для точного кодирования
- Период SMA, тип (SMA/EMA), таймфрейм у автора — неизвестны: `[ИНДИКАТОР — нужен скрипт]`.
