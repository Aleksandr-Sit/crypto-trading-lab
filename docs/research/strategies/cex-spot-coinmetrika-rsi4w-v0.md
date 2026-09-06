---
id: cex-spot-coinmetrika-rsi4w-v0
branch: cex-spot
source_kind: author_indicator
source_ref: https://t.me/s/coinmetrika
can_backtest: true
timeframe: 1w
instruments: [BTC/USDT, ETH/USDT]
venue: binance
regime: any
status: hypothesis
params:
  rsi_period: 14
  smooth_weeks: 4
  oversold: 40
  overbought: 70
  ma_filter_days: 365            # 0 — без фильтра (вторая строка замера)
  position_pct_of_branch: 50
  indicator_version: v0
---

# Coinmetrika «4-недельный индикатор / зеленка» — порт v0

## Идея
Из постов автора известны только фрагменты: «кастомный 4-недельный индикатор» с зелёными точками («зеленка») «вне зоны перекупленности», «мой пользовательский RSI», месячный TF и 365-дневная MA (источники — `docs/research/indicators/coinmetrika.md`). v0 собирает их в одну проверяемую гипотезу: сглаженный недельный RSI + фильтр годовой MA.

## Правила (кодируемые)

**Вход.** На закрытии недельной свечи: `R = SMA(RSI(rsi_period), smooth_weeks)`; «зеленка» = `R[t−1] < oversold` и `R[t] ≥ oversold` (выход снизу). Если `ma_filter_days > 0` — дополнительно `close > SMA(ma_filter_days)` по дневным. Покупка на открытии следующей недели.

**Выход.** `R[t] > overbought` → продать всё. Стоп ветки по риску (`config/limits.yaml`), своего стопа нет — сигналы редкие.

**Размер.** `position_pct_of_branch` %; одна позиция на инструмент.

**Таймфрейм и инструменты.** 1w (сетка замера: 1w, 1M); BTC, ETH.

**Издержки.** Taker; ≤ 5 сделок в год.

## Режим рынка
Циклический BTC; на 1w — единицы сигналов за историю (2017–2026), порог В12 (≥30 сделок) недостижим на одном инструменте — замер по корзине топ-20 монет, отдельная строка.

## Источники
- [t.me/s/coinmetrika](https://t.me/s/coinmetrika) — упоминания «4-недельного индикатора», «зеленки», перекупленности (превью канала, 2026-09-06).
- [telemetr.me/content/coinmetrika](https://telemetr.me/content/coinmetrika) — пост 02.09.2026: «мой пользовательский RSI… сигнал разворота», месячный TF, 365-дневная MA.
- [coinmetrika.capital](https://coinmetrika.capital/) — «13+ индикаторов… перепроданности/перекупленности».

## Чего не хватает для точного кодирования
- Всё, кроме фрагментов выше — неизвестно: `[ИНДИКАТОР — нужен скрипт]`. Параметры `rsi_period`, `smooth_weeks`, пороги — гипотеза тикета 13, не знание об индикаторе.
