---
id: cex-perp-binance-futures-grid-neutral
branch: cex-perp
source_kind: bot_preset
source_ref: https://www.binance.com/en/support/faq/what-is-futures-grid-trading-f4c453bab89648beb722aa26634120c3
can_backtest: true
timeframe: 1m
instruments: [BTCUSDT-PERP, ETHUSDT-PERP]
venue: binance
regime: range
status: hypothesis
params:
  grid_mode: neutral            # neutral | long | short
  range_atr_mult: 2.0           # диапазон = цена ± range_atr_mult * ATR(14, 1d)
  grids: 30
  leverage: 3
  margin_usd: 200
  stop_loss_pct: 8
  trailing_up: false
  trailing_down: false
---

# Фьючерсный грид (Binance, нейтральный режим)

## Идея
Грид на перпе с плечом: нейтральный режим ставит sell-уровни выше текущей цены (шорт) и buy-уровни ниже (лонг), не открывая стартовую позицию. Профит — с колебаний; фандинг — в издержках обеих сторон.

## Правила (кодируемые)

**Вход.** Диапазон `[P − k·ATR, P + k·ATR]`, `k = range_atr_mult`, ATR(14) по дневным свечам на момент старта. Шаг арифметический. Neutral: уровни выше `P` — лимит-sell (открывают/наращивают шорт), ниже — лимит-buy (лонг); исполненный ордер ставит противоположный на соседнем уровне (Binance FAQ: «neutral grid… sell orders above, buy orders below current price»).

**Выход.** Стоп: убыток по позиции ≥ `stop_loss_pct` от `margin_usd` → закрыть всё, стоп бота. Выход за диапазон без trailing — бот стоит с открытой позицией до возврата или стопа.

**Размер.** Маржа `margin_usd`, плечо `leverage` (≤ лимит ветки `cex-perp` в `config/limits.yaml`); номинал на уровень = `margin_usd·leverage/grids`. Проверка R20.1: цена ликвидации должна быть дальше стопа.

**Таймфрейм и инструменты.** 1m; инструменты — BTC/ETH перпы (глубокий стакан).

**Издержки.** Maker на каждом уровне, фандинг каждые 8 ч по открытой нетто-позиции, проскальзывание при стопе (taker).

## Режим рынка
Боковик с волатильностью; ломается в тренде (накапливается позиция против движения + фандинг против).

## Источники
- [Binance FAQ: What Is Futures Grid Trading](https://www.binance.com/en/support/faq/what-is-futures-grid-trading-f4c453bab89648beb722aa26634120c3) — режимы, механика.
- [Binance FAQ: Long/Short grid](https://www.binance.com/en/support/faq/what-is-long-short-grid-trading-904e47602a3941b99e960a31e152a986); [Trailing up/down](https://www.binance.com/en/support/faq/how-to-use-the-trailing-up-and-trailing-down-functions-in-usd%E2%93%A2-m-futures-grid-trading-7a7bb22420404385991dee3a0930207d).

## Чего не хватает для точного кодирования
- Binance не задаёт дефолтный диапазон — правило `±2·ATR(14,1d)` принято тикетом 13.
- Trailing up/down — отдельная строка замера (`trailing_up: true`), логика переноса сетки по FAQ: при выходе цены за верх верхняя граница сдвигается, нижние уровни отменяются.
