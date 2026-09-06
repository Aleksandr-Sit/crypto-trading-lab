---
id: cex-perp-turtle-donchian
branch: cex-perp
source_kind: book
source_ref: https://oxfordstrat.com/coasdfASD32/uploads/2016/01/turtle-rules.pdf
can_backtest: true
timeframe: 1d
instruments: [BTCUSDT-PERP, ETHUSDT-PERP, SOLUSDT-PERP, BNBUSDT-PERP, XRPUSDT-PERP]
venue: binance
regime: trend
status: hypothesis
params:
  system: 2                      # 1 — 20/10 дней с фильтром последней сделки; 2 — 55/20 без фильтра
  entry_days: 55
  exit_days: 20
  atr_period: 20                 # «N» — 20-дневная EMA true range
  stop_atr_mult: 2.0
  risk_unit_pct: 1.0             # 1 unit = 1 % капитала на 1N движения
  max_units_per_market: 4
  pyramid_step_atr: 0.5
  leverage_cap: 2
---

# Черепахи: пробой канала Дончиана (System 2, 55/20)

## Идея
Оригинальные правила Turtle Trading (Деннис/Экхардт): вход на пробое N-дневного максимума/минимума, выход на пробое противоположного канала меньшей длины, размер от волатильности («N»). Трендследящая система на портфеле инструментов.

## Правила (кодируемые)

**Вход.** Long: `close > max(high[−entry_days..−1])`; short: `close < min(low[...])` (System 2: без фильтра «прошлая сделка была прибыльной»). Вход на открытии следующего дня. Пирамидинг: добавлять 1 unit на каждые `pyramid_step_atr·N` в сторону прибыли, до `max_units_per_market`.

**Выход.** Стоп `entry ∓ stop_atr_mult·N` (единый стоп на все units по цене последнего добавления). Выход по правилу: long — `close < min(low[−exit_days..−1])`, short — зеркально.

**Размер.** `N = EMA(TR, atr_period)`; `unit = capital·risk_unit_pct/100 / N` (в единицах контракта, `N` в USD на контракт). Ограничения оригинала: ≤4 units на рынок, ≤6 в тесно коррелированных, ≤12 в одну сторону. Плечо ≤ `leverage_cap`.

**Таймфрейм и инструменты.** 1d; 5 перпов с наибольшим OI (Binance).

**Издержки.** Taker вход/выход (стоп/пробой рыночно), фандинг на удержании (часто недели), проскальзывание.

## Режим рынка
Тренд; в боковике серия убытков по −2N с win-rate ~35–40 % (оригинальные черепахи).

## Источники
- [The Original Turtle Trading Rules (PDF, Curtis Faith)](https://oxfordstrat.com/coasdfASD32/uploads/2016/01/turtle-rules.pdf) — System 1/2, N, units, стопы 2N, пирамидинг ½N, лимиты units.
- [Trading Blox: Turtle System Rules](https://www.tradingblox.com/Manuals/UsersGuideHTML/turtlesystem.htm) — та же формализация в виде параметров.

## Чего не хватает для точного кодирования
- Оригинал торговал фьючерсы с дневными лимитами движения — для крипты правило «пропуск входа при limit-move» не применяется (решение тикета 13).
