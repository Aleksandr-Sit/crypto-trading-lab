---
id: meme-sol-smartmoney-follow
branch: meme
source_kind: api_docs
source_ref: https://www.helius.dev/docs/billing/plans
can_backtest: false
timeframe: event
instruments: "токены Solana, купленные ≥ N отслеживаемыми кошельками"
venue: jupiter
regime: high_vol
status: hypothesis
params:
  tracked_wallets_source: candidates   # кошельки kind: wallet, chain: solana из очереди discovery
  min_wallets_confirming: 2
  confirm_window_s: 300
  min_wallet_buy_usd: 500
  honesty_checklist: same_as_migration_v1
  size_usd: 10
  tp1_mult: 2.0
  tp1_sell_pct: 50
  trailing_stop_pct: 35
  hard_stop_pct: 50
  max_hold_min: 720
  exit_on_wallets_sell_pct: 50      # если ≥50 % подтвердивших кошельков продали — выход
  max_open: 5
---

# Следование за смарт-мани кошельками на Solana (мемы)

## Идея
Покупка нового токена сразу несколькими кошельками с подтверждённой историей — сигнал сильнее любого индикатора цены. Кошельки — из очереди discovery (пересчёт по сделкам, R22), поток сделок — Helius Enhanced Transactions / webhooks или Cielo `/feed`.

## Правила (кодируемые)

**Вход.** Поток свопов отслеживаемых кошельков. Если ≥ `min_wallets_confirming` разных кошельков купили один mint в окне `confirm_window_s`, каждый на ≥ `min_wallet_buy_usd`, и токен проходит чек-лист честности (как в `meme-sol-pumpfun-migration-v1`) → своп `size_usd` через Jupiter.

**Выход.** Лестница (tp1 + трейлинг), жёсткий стоп, стоп по времени; дополнительно — если доля подтвердивших кошельков, продавших ≥ 50 % своей позиции, ≥ `exit_on_wallets_sell_pct` % → продать всё.

**Размер.** `size_usd`; ≤ `max_open`.

**Таймфрейм и инструменты.** События; лаг копирования измеряется (5/30/120 с — R22.2).

**Издержки.** Fee пула, priority fee, газ, проскальзывание, неудачные транзакции.

## Режим рынка
Мем-фаза; ломается, когда отслеживаемые кошельки сами становятся «ведущими» (front-running их последователей) — детектируется падением их пересчитанного PnL (R08.3).

## Источники
- [Helius plans](https://www.helius.dev/docs/billing/plans) — Enhanced Transactions (парсинг свопов), webhooks, 1M кредитов.
- Cielo `/feed` — 5000 кредитов/мес (см. `data-sources.md` §3).
- Методики отбора кошельков: [Nansen guide](https://nansen.ai/post/how-to-track-solana-wallets-complete-guide-for-smart-money-analysis), [GMGN blog](https://gmgn.ai/blog/how-to-track-copy-solana-smart-money/) — без чисел; пороги — выбор тикета 13.

## Чего не хватает для точного кодирования
- Список кошельков пуст на старте (см. `candidates/seed.md`) — стратегия активируется после первого прохода discovery.

## Вердикт: проверена живыми деньгами в соседнем проекте — отвергнута (запись 03.10.2026)

Ровно эту стратегию (конфлюенс ≥2 отслеживаемых кошельков, клип $10, мемы Solana) проект
`smart-money` довёл до живых денег: **176 сделок в двух окнах (10.08 и 13.08.2026) — −$0.72
на сделку**, разрыв исполнения 0.886 при пороге окупаемости 0.911. Все три рычага починки
(отбор по признакам входа, правило выхода, скорость) замерены и отвергнуты: признак,
предсказывающий крупный выигрыш, предсказывает и крупный провал исполнения. Круг издержек
pump.fun при клипе $10 — медиана 5.54%, среднее 8.61% (отсюда поправка `pumpfun` в
`config/costs.yaml`). Отбор кошельков на будущем периоде тоже не устоял (10.07): у
селективных — 0.000 вне выборки, устойчивы только спрей-боты.

Источник — `smart-money/docs/live_readiness.md` (репозиторий `Aleksandr-Sit/smart-money`),
данные — архив `Торговля/lab-data/archive/smart-money-2026-10-03.tar.gz`. В замер не брать;
адреса, засеянные `scripts/import_wallets.py` из smart-money, — боты и ротации кошельков.
Сводка — `docs/research/sibling-projects-2026-10-03.md`.
