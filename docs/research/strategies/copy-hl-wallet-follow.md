---
id: copy-hl-wallet-follow
branch: copy
source_kind: api_docs
source_ref: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
can_backtest: true
timeframe: event
instruments: "перпы Hyperliquid, торгуемые лидером"
venue: hyperliquid
regime: any
status: hypothesis
params:
  leader_min_account_value_usd: 100000
  leader_min_fills_30d: 50
  leader_min_age_days: 60
  leader_min_month_pnl_pct: 5
  poll_interval_s: 10
  copy_scale_pct_of_leader_equity: 1.0
  max_position_pct_of_branch: 10
  max_leverage: 3
  copy_lag_s: 5
  orphan_close_after_s: 120
---

# Следование за кошельком Hyperliquid по `userFills`

## Идея
Все филлы любого адреса на Hyperliquid публичны без ключа. Отбираем адреса из лидерборда/своих кандидатов по собственному пересчёту (R22), повторяем их сделки с масштабом и лагом.

## Правила (кодируемые)

**Вход.** Отбор раз в неделю: `clearinghouseState(address).marginSummary.accountValue ≥ leader_min_account_value_usd`, `userFillsByTime` за 30 дней ≥ `leader_min_fills_30d`, возраст первого филла ≥ `leader_min_age_days`, PnL за месяц по собственному пересчёту ≥ `leader_min_month_pnl_pct`. Раз в `poll_interval_s`: новые филлы лидера (`userFillsByTime(startTime=last_seen)`), открывающие/наращивающие позицию → наш ордер той же стороны на `notional = наш_капитал·(notional_филла/accountValue_лидера)·copy_scale/1.0`.

**Выход.** Филлы лидера, уменьшающие позицию, → пропорциональное сокращение; при полном закрытии — закрыть. Осиротевшая позиция (не удалось закрыть за `orphan_close_after_s`) — рыночно; стоп −10 %.

**Размер.** ≤ `max_position_pct_of_branch` %, плечо ≤ 3, R20.1.

**Таймфрейм и инструменты.** События; бэктест — по истории `userFills` (≤10 000 последних) с ценами 1m (`candleSnapshot`) и лагом 5/30/120 с.

**Издержки.** HL taker 4,5 bps, фандинг часовой, проскальзывание, `copy_lag_cost`.

## Режим рынка
Зависит от лидера; еженедельное переизмерение (R08.3).

## Источники
- [Hyperliquid API: Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) — `userFills`, `userFillsByTime`, `clearinghouseState`, `candleSnapshot`.
- [Rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits) — 1200 weight/мин; опрос 10 с × N лидеров укладывается при N ≤ 10.
- Лидерборд — `stats-data.hyperliquid.xyz/Mainnet/leaderboard` (неофициальный, см. `data-sources.md` §2).

## Чего не хватает для точного кодирования
- Адреса-кандидаты не собраны (см. `candidates/seed.md`, раздел Hyperliquid) — discovery (тикет 12) заполняет.
