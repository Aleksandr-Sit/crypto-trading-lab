---
id: cex-perp-basis-cash-carry
branch: cex-perp
source_kind: api_docs
source_ref: https://www.binance.com/en/support/faq/what-are-binance-delivery-futures-contracts-360033525111
can_backtest: true
timeframe: 1h
instruments: [BTCUSDT-QUARTERLY, ETHUSDT-QUARTERLY]
venue: binance
regime: any
status: hypothesis
params:
  entry_basis_annualized_pct: 10
  exit_basis_annualized_pct: 2
  hold_to_expiry: true
  leverage: 1
  max_notional_pct_of_branch: 50
---

# Cash-and-carry: спот-лонг + шорт квартального фьючерса

## Идея
Квартальный фьючерс торгуется с премией к споту (контанго); шорт фьючерса против спот-лонга фиксирует премию к экспирации без фандинга.

## Правила (кодируемые)

**Вход.** Каждый час: `basis_ann = (F − S)/S · 365/days_to_expiry · 100`. Если `basis_ann > entry_basis_annualized_pct` и `days_to_expiry ≥ 14` — купить спот, продать фьючерс на равный notional.

**Выход.** `hold_to_expiry: true` — держать до расчёта (фьючерс сходится к индексу); досрочно — если `basis_ann < exit_basis_annualized_pct` (премия выбрана раньше) или `basis_ann < 0` в течение 24 ч (риск).

**Размер.** `notional ≤ max_notional_pct_of_branch` % капитала ветки; плечо 1, маржа = notional.

**Таймфрейм и инструменты.** 1h; квартальные USDT-M контракты BTC/ETH (Binance) — история из data.binance.vision (`futures/um` по символам `BTCUSDT_YYMMDD`).

**Издержки.** 2–4 taker-сделки; фандинга нет; проскальзывание на фьючерсе (тоньше стакан).

## Режим рынка
Бычий рынок с контанго; в бэквордации сигналов нет.

## Источники
- [Binance FAQ: Delivery futures contracts](https://www.binance.com/en/support/faq/what-are-binance-delivery-futures-contracts-360033525111) — квартальные контракты, расчёт по индексу на экспирации (URL не перепроверен 2026-09-06 (?)).
- [OKX Docs v5: Futures](https://www.okx.com/docs-v5/en/) — альтернативная площадка с квартальными FUTURES-инструментами (`instType=FUTURES`).

## Чего не хватает для точного кодирования
- Ролл между кварталами при `hold_to_expiry: false` не описан — тикет 07 не реализует ролл в v0.
