---
id: nft-mint-priority-v1
branch: nft
source_kind: spec
source_ref: https://docs.magiceden.io/reference/solana-overview
can_backtest: false
timeframe: event
instruments: "публичные минты launchpad Magic Eden с ценой ≤ max_price_sol"
venue: magiceden
regime: high_vol
status: hypothesis
params:
  max_price_sol: 0.5
  min_creator_score: 0.5          # рейтинг создателя из R23 (0..1); 0 — без фильтра
  min_attention_index: 0.3        # индекс внимания R23.2 (0..1)
  send_offset_ms: 0               # отправка ровно в момент открытия; варианты: -200, +500
  priority_fee_sol: 0.002         # варианты: 0.0005, 0.002, 0.01
  wallets: 1                      # варианты: 1, 3
  max_attempts: 3
  sell_rule: same_as_early_secondary
---

# Публичный минт с вариантами приоритета (G09 / G09.1)

## Идея
Участие в публичном минте — лотерея с измеримыми параметрами: доля успешных попыток, время до подтверждения, стоимость неудач. Каждая комбинация (priority fee × момент отправки × число кошельков) — своя строка замера `nft-mint-*`.

## Правила (кодируемые)

**Вход.** Лента предстоящих минтов (launchpad + парсинг календарей, R23.1) → фильтр: `price ≤ max_price_sol`, `creator_score ≥ min_creator_score`, `attention_index ≥ min_attention_index`. В `launchDate + send_offset_ms` отправить транзакцию минта с `priority_fee_sol` от `wallets` кошельков; при отказе — повтор до `max_attempts` с fee ×2.

**Выход.** После минта — правила `nft-early-secondary-floor-v1` (лестница, неликвид).

**Размер.** 1 минт на кошелёк на коллекцию; лимит ветки.

**Таймфрейм и инструменты.** События; метрики `mint_attempts`, `mint_success_rate`, `mint_cost_failed`, `confirm_latency_ms`.

**Издержки.** Цена минта, priority fee (в т. ч. за неудачные), газ, при продаже — комиссия + роялти.

## Режим рынка
Хайп-фаза коллекций; ломается при ботовой конкуренции (нулевой success rate при низком fee) — что и покажет замер.

## Источники
- [Magic Eden API: launchpad collections](https://docs.magiceden.io/reference/solana-overview) — `launchDate`, `price`, `size` (проверено 2026-09-05; 15 коллекций в `candidates/seed.md`).
- Спец. истории 74–77 (R23.1, R23.2, R23.3, G09, G09.1).

## Чего не хватает для точного кодирования
- Транзакция минта зависит от программы launchpad (Candy Machine v3 / ME launchpad) — интерфейс `NftMarket.mint()` тикета 10; allowlist-минты — по отметке оператора (R23.3).
