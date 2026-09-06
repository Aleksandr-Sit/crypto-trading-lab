---
id: cex-perp-funding-arb-spot-hedge
branch: cex-perp
source_kind: api_docs
source_ref: https://www.binance.com/en/support/faq/introduction-to-binance-futures-funding-rates-360033525031
can_backtest: true
timeframe: 1h
instruments: [BTC, ETH, SOL]
venue: binance
regime: any
status: hypothesis
params:
  entry_funding_annualized_pct: 15     # войти, если прогнозный фандинг ×3×365 > 15 % годовых
  exit_funding_annualized_pct: 3
  min_hold_funding_intervals: 3
  leverage: 1
  max_notional_pct_of_branch: 50
  rebalance_threshold_pct: 2           # выравнивать хедж, если дельта > 2 %
---

# Фандинг-арбитраж: спот-лонг + перп-шорт

## Идея
При положительном фандинге шорт перпа получает выплату каждые 8 ч; спот-лонг того же актива нейтрализует цену. Доход = фандинг − комиссии − расхождение базиса.

## Правила (кодируемые)

**Вход.** Каждый час: `f = predicted_funding_rate` (Binance `premiumIndex.lastFundingRate`). Если `f·3·365·100 > entry_funding_annualized_pct` — купить спот на `notional` и открыть шорт перпа на тот же `notional` (плечо 1, изолированная маржа = notional).

**Выход.** Если аннуализированный фандинг < `exit_funding_annualized_pct` после ≥ `min_hold_funding_intervals` выплат — закрыть обе ноги. Также выход при базисе (perp − spot)/spot < −1 % (риск обратного фандинга).

**Размер.** `notional ≤ max_notional_pct_of_branch` % капитала ветки на актив; при дельте между ногами > `rebalance_threshold_pct` — довыравнивание.

**Таймфрейм и инструменты.** 1h свечи + история фандинга (data.binance.vision `fundingRate`); активы с фандингом и спотом на одной бирже.

**Издержки.** 4 taker-сделки на цикл (2 ноги × вход/выход), фандинг — доход, проскальзывание; занятая маржа не приносит дохода.

## Режим рынка
Перегретый бычий рынок (фандинг высок); в медвежьем/нейтральном — сигналов нет. Риск: ликвидация шорт-ноги при резком росте, если маржа изолирована — держать маржу = 100 % notional.

## Источники
- [Binance FAQ: Introduction to Funding Rates](https://www.binance.com/en/support/faq/introduction-to-binance-futures-funding-rates-360033525031) — период 8 ч, формула (premium + interest), оплата между лонгами и шортами.
- [Hyperliquid docs: Funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding) — часовой фандинг, аналог для `dex-perp` (см. `dex-perp-hl-funding-carry`).

## Чего не хватает для точного кодирования
- Порог 15 % годовых — выбор тикета 13; замер по сетке {10, 15, 25}.
