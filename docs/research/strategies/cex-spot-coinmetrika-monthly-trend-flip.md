---
id: cex-spot-coinmetrika-monthly-trend-flip
branch: cex-spot
source_kind: author_indicator
source_ref: .autopilot/2026-09-05-crypto-trading-lab--wip/user-inputs/coinmetrika-2-btc-1m-trend-rsi.png
can_backtest: true
timeframe: 1M
instruments: [BTC/USDT, ETH/USDT]
venue: binance
regime: trend
status: hypothesis
params:
  trend_model: supertrend          # гипотеза формы «индикатора тренда» с фоном и метками BUY/SELL; варианты: supertrend | ema_cross
  supertrend_atr_period: 10
  supertrend_mult: 3.0
  ema_fast: 3                      # для варианта ema_cross
  ema_slow: 10
  rsi_period: 14
  rsi_fast_smooth: 3               # «пользовательский RSI» — две сглаженные линии (гипотеза: EMA 3 и EMA 8 от RSI)
  rsi_slow_smooth: 8
  oversold: 20
  overbought: 80
  rsi_confirm_window_months: 3     # RSI должен выйти из зоны 20 вверх не ранее чем за 3 месяца до/после флипа тренда
  exit_mode: trend_flip            # trend_flip — по метке SELL; rsi_overbought — по выходу RSI из зоны 80 вниз
  stop_loss_pct: 30
  position_pct_of_branch: 50
  indicator_version: v0
---

# Coinmetrika: месячный разворот тренда + RSI из зоны 20 — порт v0

## Идея
По скриншоту автора (BTC 1M): индикатор тренда меняет цвет фона и ставит BUY/SELL на разворотах; одновременно «пользовательский RSI» (две сглаженные линии, зоны 80/20) выходит из зоны 20. Такое совпадение автор отмечает **01.06.2015, 01.04.2019, 01.01.2023, 01.08.2026** — «во всех трёх случаях дно цикла уже осталось позади». Гипотеза: вход на совпадении двух месячных сигналов, удержание до противоположного.

## Правила (кодируемые)

**Вход.** На закрытии месячной свечи: `trend` — направление Supertrend(`supertrend_atr_period`, `supertrend_mult`) на 1M (или `EMA(ema_fast) > EMA(ema_slow)` при `ema_cross`); флип вверх = `trend[t] = up` и `trend[t−1] = down` (метка BUY). `rsi_fast = EMA(RSI(rsi_period), rsi_fast_smooth)`; сигнал RSI = `rsi_fast` пересекает `oversold` снизу вверх. Вход на открытии следующего месяца, если оба сигнала произошли в пределах `rsi_confirm_window_months` друг от друга (любой порядок).

**Выход.** `exit_mode: trend_flip` — флип тренда вниз (метка SELL) → продать всё на открытии следующего месяца; `rsi_overbought` — `rsi_fast` пересекает `overbought` сверху вниз. Стоп `entry·(1 − stop_loss_pct/100)` на закрытии месяца.

**Размер.** `position_pct_of_branch` % капитала ветки; одна позиция на инструмент.

**Таймфрейм и инструменты.** 1M (агрегация из дневных свечей); BTC (история Bitstamp с 2011 доступна через data.binance.vision только с 2017 — для теста дат 2015/2019 нужен внешний ряд BTC/USD, например Bitstamp API), ETH. Сетка замера: `trend_model`, `exit_mode`.

**Издержки.** Taker; 1–3 сделки за цикл.

## Режим рынка
Многолетние циклы BTC; в боковике — пила месячных флипов (на скриншоте: сдвоенные BUY BUY / SELL SELL в 2014–2015, 2019–2020) — именно там стратегия теряет.

## Источники
- Скриншот пользователя [`coinmetrika-2-btc-1m-trend-rsi.png`](../../../.autopilot/2026-09-05-crypto-trading-lab--wip/user-inputs/coinmetrika-2-btc-1m-trend-rsi.png) — фон тренда, метки BUY/SELL, две линии RSI с зонами 80/20, даты 01.06.2015 / 01.04.2019 / 01.01.2023 / 01.08.2026, отрезки 16/14/13/11 месяцев; описание — [`user-inputs/README.md`](../../../.autopilot/2026-09-05-crypto-trading-lab--wip/user-inputs/README.md).
- Пост автора 02.09.2026 «индикатор тренда изменил направление, и мой пользовательский RSI демонстрирует тот же сигнал разворота» — [telemetr.me/content/coinmetrika](https://telemetr.me/content/coinmetrika).
- Разбор — `docs/research/indicators/coinmetrika.md`.
- Supertrend — Olivier Seban; реализация TradingView `ta.supertrend(factor, atrPeriod)`.

## Чего не хватает для точного кодирования
- Формулы «индикатора тренда» и «пользовательского RSI» неизвестны — `[ИНДИКАТОР — нужен скрипт]`; Supertrend(10, 3) и EMA(3)/EMA(8) от RSI(14) — гипотеза тикета 13.
- Тест v1: метки BUY в месяцы 2015-06, 2019-04, 2023-01, 2026-08 (±1 месяц) и одновременный выход RSI из зоны 20; метки SELL — 2014, 2018, 2021-05/2021-12, 2025 (визуально).
