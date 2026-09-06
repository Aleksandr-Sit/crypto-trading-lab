---
id: prediction-pm-copy-leaderboard
branch: prediction
source_kind: api_docs
source_ref: https://docs.polymarket.com/quickstart/introduction/rate-limits
can_backtest: true
timeframe: event
instruments: "рынки Polymarket, где открывает позиции лидер"
venue: polymarket
regime: any
status: hypothesis
params:
  leaderboard_period: MONTH
  top_n: 15
  min_pnl_to_volume: 0.05        # отсекает маркет-мейкеров
  min_closed_positions: 30
  poll_interval_s: 60
  copy_size_usd: 5
  max_open_markets: 10
  skip_if_price_moved_pct: 3     # не копировать, если цена ушла > 3 п.п. от цены лидера
  exit_rule: follow_or_resolve   # закрывать вслед за лидером; иначе — держать до резолюции
---

# Копирование топ-кошельков Polymarket (`pm-copy-*`)

## Идея
Лидерборд и позиции каждого кошелька Polymarket публичны (Data API). Копируем новые позиции лидеров с фильтром «PnL/оборот» (отсекает маркет-мейкеров) и меряем в вероятностях: Brier и доходность к резолюции (G02.1).

## Правила (кодируемые)

**Вход.** Еженедельно: `/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=top_n` → оставить `pnl/vol ≥ min_pnl_to_volume` и `closed-positions ≥ min_closed_positions`. Раз в `poll_interval_s`: `/activity?user=<proxyWallet>` → новая покупка `BUY` outcome-токена → наша лимитная покупка `copy_size_usd` по текущему ask CLOB, если `|ask − цена_лидера| ≤ skip_if_price_moved_pct` п.п.

**Выход.** `exit_rule: follow_or_resolve` — если лидер продал (`SELL` в activity) — продать по bid; иначе держать до резолюции рынка (выплата 1 или 0).

**Размер.** `copy_size_usd` на рынок, ≤ `max_open_markets` рынков одновременно.

**Таймфрейм и инструменты.** События; бэктест — по `/trades?user=` лидера (история) и `/prices-history` CLOB.

**Издержки.** Комиссий CLOB 0 (`config/costs.yaml`), газ Polygon ~0,05 USD, спред стакана; `copy_lag_cost`.

## Режим рынка
Не зависит от крипторынка; ломается на рынках с низкой ликвидностью (спред > 5 п.п.). Если торговля недоступна с IP — режим «только замер» (G02.2).

## Источники
- [Polymarket Data API rate limits](https://docs.polymarket.com/quickstart/introduction/rate-limits) — `/positions`, `/trades`, `/activity`, `/v1/leaderboard`; проверено 2026-09-06 (15 кошельков в `candidates/seed.md`).
- [Polymarket CLOB docs](https://docs.polymarket.com/developers/CLOB/introduction) — стакан, `/prices-history`, `py-clob-client`.

## Чего не хватает для точного кодирования
- Определение `min_closed_positions` через `/closed-positions` — пагинация не проверена.
