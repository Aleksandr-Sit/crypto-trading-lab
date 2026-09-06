---
id: dex-perp-hl-funding-carry
branch: dex-perp
source_kind: api_docs
source_ref: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
can_backtest: true
timeframe: 1h
instruments: [BTC, ETH, SOL, HYPE]
venue: hyperliquid
regime: any
status: hypothesis
params:
  entry_spread_annualized_pct: 10     # |funding_HL − funding_Binance| в годовых
  exit_spread_annualized_pct: 2
  min_hold_hours: 8
  leverage: 2
  max_notional_pct_of_branch: 40
  delta_rebalance_pct: 2
---

# Фандинг-спред Hyperliquid vs Binance (perp-perp)

## Идея
Фандинг на Hyperliquid платится каждый час, на Binance — каждые 8 ч; ставки расходятся. Лонг на площадке с низким/отрицательным фандингом, шорт — с высоким; дельта-нейтрально.

## Правила (кодируемые)

**Вход.** Каждый час: `s = (f_HL·24 − f_BN·3)·365·100` (годовые; `f_HL` — часовая ставка из `/info` `metaAndAssetCtxs.funding`, `f_BN` — 8-часовая). Если `s > entry_spread_annualized_pct` — шорт HL + лонг Binance на равный notional; если `s < −entry…` — зеркально.

**Выход.** `|s| < exit_spread_annualized_pct` после ≥ `min_hold_hours` — закрыть обе ноги; также при расхождении цен ног > 1,5 % (базис-риск).

**Размер.** `notional ≤ max_notional_pct_of_branch` %; плечо ≤ 2 на обеих; дельта выравнивается при отклонении > `delta_rebalance_pct`. R20.1 — ликвидация дальше стопа.

**Таймфрейм и инструменты.** 1h; активы, торгуемые на обеих площадках. История фандинга HL — `fundingHistory` (info API), Binance — архивы.

**Издержки.** HL taker 4,5 bps / maker 1,5 bps (`config/costs.yaml`), Binance 10 bps; 4 сделки на цикл; фандинг обеих ног.

## Режим рынка
Любой с расхождением ставок; наиболее часто — в перегретые дни. Ломается при резком движении, если одна нога ликвидируется раньше выравнивания.

## Источники
- [Hyperliquid docs: Funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding) — часовая выплата, формула premium с capping.
- [Hyperliquid API: Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) — `metaAndAssetCtxs` (funding, openInterest), `fundingHistory`.
- [Binance FAQ: Funding Rates](https://www.binance.com/en/support/faq/introduction-to-binance-futures-funding-rates-360033525031).

## Чего не хватает для точного кодирования
- Порог 10 % годовых — выбор тикета 13.
