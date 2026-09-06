---
id: cex-spot-tsmom-weekly
branch: cex-spot
source_kind: paper
source_ref: https://doi.org/10.1016/j.jfineco.2011.11.003
can_backtest: true
timeframe: 1d
instruments: [BTC/USDT, ETH/USDT]
venue: binance
regime: trend
status: hypothesis
params:
  lookback_days: 84              # 12 недель
  rebalance_days: 7
  vol_target_annual_pct: 40
  vol_lookback_days: 30
  max_leverage: 1.0              # спот — без плеча, только доля 0..1
  allow_short: false
---

# Time-series momentum (Moskowitz–Ooi–Pedersen) на BTC/ETH

## Идея
Знак доходности за прошлые 12 недель предсказывает знак следующей; позиция масштабируется к целевой волатильности. Академическая стратегия, воспроизведённая на крипте (см. источники).

## Правила (кодируемые)

**Вход.** Каждые `rebalance_days` дней: `r = close[t]/close[t−lookback_days] − 1`. Если `r > 0` — long с весом `w = min(max_leverage, vol_target/σ_ann)`, где `σ_ann` — годовая волатильность дневных доходностей за `vol_lookback_days`. Если `r ≤ 0` — вес 0 (спот, `allow_short: false`) — в USDT.

**Выход.** Только на ребалансе: изменение веса до нового `w` (в т. ч. до 0).

**Размер.** Вес `w` от капитала ветки на инструмент, поровну между BTC и ETH.

**Таймфрейм и инструменты.** 1d; BTC, ETH (история ≥ 5 лет).

**Издержки.** Taker на ребалансе по разнице весов; проскальзывание.

## Режим рынка
Трендовые годы; ломается на резких разворотах (v-образных) — фиксирует убыток от лага 12 недель.

## Источники
- Moskowitz, Ooi, Pedersen (2012), «Time series momentum», Journal of Financial Economics 104(2) — [DOI](https://doi.org/10.1016/j.jfineco.2011.11.003): правило sign(12-мес. доходности), масштабирование к волатильности.
- Liu, Tsyvinski, Wu (2022), «Common Risk Factors in Cryptocurrency», Journal of Finance — [DOI](https://doi.org/10.1111/jofi.13119): моментум-фактор на крипте (1–4-недельные горизонты).

## Чего не хватает для точного кодирования
- Горизонт 12 недель вместо 12 месяцев — адаптация тикета 13 к короткой истории крипты; сетка замера lookback ∈ {28, 56, 84, 168} дней.
