---
id: copy-okx-lead-filtered
branch: copy
source_kind: api_docs
source_ref: https://www.okx.com/docs-v5/en/#order-book-trading-copy-trading-get-lead-traders-ranks
can_backtest: true
timeframe: event
instruments: "SWAP-инструменты лидера"
venue: okx
regime: any
status: hypothesis
params:
  min_lead_days: 90
  min_copiers: 10
  min_win_ratio: 0.55
  max_aum_usd: 5000000
  min_aum_usd: 5000
  poll_interval_s: 30
  copy_scale: 0.01               # доля от размера лидера относительно его AUM → наш капитал
  max_position_pct_of_branch: 10
  max_leverage: 3
  copy_lag_s: 5
  drop_if_weekly_pnl_negative_weeks: 3
---

# Копирование лид-трейдера OKX с фильтрами (публичный API)

## Идея
OKX — единственный CEX с публичными эндпоинтами copy-trading без auth: список лидеров, статистика, текущие и исторические субпозиции. Фильтруем накрутку по правилам R22.1, копируем позиции с масштабом по капиталу и меряем `copy_lag_cost` (R22.2).

## Правила (кодируемые)

**Вход.** Еженедельно: `public-lead-traders?instType=SWAP&sortType=pnl` → кандидаты с `leadDays ≥ min_lead_days`, `copyTraderNum ≥ min_copiers`, `winRatio ≥ min_win_ratio`, `min_aum ≤ aum ≤ max_aum`. Раз в `poll_interval_s` — `public-current-subpositions?uniqueCode=…`; новая субпозиция лидера → наш ордер той же стороны на `notional = наш_капитал_ветки · (notional_лидера / aum_лидера) · copy_scale/0.01`, с задержкой `copy_lag_s` (моделируется в бэктесте по `public-subpositions-history` с ценами 1m).

**Выход.** Лидер закрыл субпозицию (пропала из current / появилась в history) → закрыть нашу. «Висящая» (R08.2): если не закрылась за 60 с — рыночно; стоп −10 % от входа на нашу позицию независимо от лидера.

**Размер.** ≤ `max_position_pct_of_branch` % на позицию, плечо ≤ `max_leverage`.

**Таймфрейм и инструменты.** События; инструменты — те, что торгует лидер и есть у нас (иначе пропуск с записью).

**Издержки.** Taker, фандинг, проскальзывание; плюс `copy_lag_cost` по задержкам 5/30/120 с.

## Режим рынка
Зависит от лидера; лидер снимается при отрицательном недельном PnL `drop_if_weekly_pnl_negative_weeks` недель подряд (`public-weekly-pnl`, R08.3).

## Источники
- [OKX API v5: Copy trading — public lead traders ranks](https://www.okx.com/docs-v5/en/#order-book-trading-copy-trading-get-lead-traders-ranks); `public-weekly-pnl`, `public-stats`, `public-current-subpositions`, `public-subpositions-history` — проверено 2026-09-05/06 (см. `docs/research/data-sources.md` §3).
- Стартовые лидеры — `candidates/seed.md` (9 uniqueCode со снимком 2026-09-06).

## Чего не хватает для точного кодирования
- Глубина `public-subpositions-history` (сколько недель) не проверена — если < 90 дней, бэктест короче порога, замер начинается с paper.
