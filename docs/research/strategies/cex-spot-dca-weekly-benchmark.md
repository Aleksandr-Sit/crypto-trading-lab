---
id: cex-spot-dca-weekly-benchmark
branch: cex-spot
source_kind: bot_preset
source_ref: https://www.binance.com/en/support/faq/what-is-binance-auto-invest-and-how-does-it-work-3be89bf23a3d4c7aa0ffd6dd5a3f6a4c
can_backtest: true
timeframe: 1d
instruments: [BTC/USDT, ETH/USDT]
venue: binance
regime: any
status: hypothesis
params:
  amount_usd: 25
  period_days: 7
  weekday: 1                     # понедельник
  allocation: {BTC/USDT: 0.7, ETH/USDT: 0.3}
  sell_rule: never
---

# Регулярная покупка (DCA-бенчмарк, Binance Auto-Invest)

## Идея
Фиксированная сумма раз в неделю без условий — контрольная стратегия: любая «умная» DCA-гипотеза должна побить её после издержек (В12: сравнение с BTC B&H — этот бенчмарк дополняет).

## Правила (кодируемые)

**Вход.** Каждые `period_days` дней (в `weekday`) рыночная покупка `amount_usd` по долям `allocation` по цене открытия дневной свечи.

**Выход.** `sell_rule: never` — позиция не продаётся; результат — mark-to-market на конец окна замера.

**Размер.** `amount_usd` за период; лимит ветки не превышается по построению.

**Таймфрейм и инструменты.** 1d; BTC/ETH.

**Издержки.** Taker-комиссия на каждую покупку; проскальзывание по стакану.

## Режим рынка
Любой; проигрывает лампсам в бычьем рынке, выигрывает у них в медвежьем — по определению.

## Источники
- [Binance FAQ: Auto-Invest](https://www.binance.com/en/support/faq/what-is-binance-auto-invest-and-how-does-it-work-3be89bf23a3d4c7aa0ffd6dd5a3f6a4c) — периодическая покупка с распределением по монетам (URL не перепроверен на 2026-09-06 (?); продукт описан в разделе Binance Earn → Auto-Invest).

## Чего не хватает для точного кодирования
- Ничего; параметры полностью заданы.
