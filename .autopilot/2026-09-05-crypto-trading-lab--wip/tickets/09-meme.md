# 09 — Мем-коины и ранние стадии: потоки, проверка честности, DEX-исполнители

**Требования:** R17, R17.1, R17.2, R17.4, R17.5, R24, A05, G07
**Blocked by:** 08
**Зона:** `src/lab/feeds/dex/` (PumpPortal, DexScreener, сабграфы Uniswap/Pancake, STON.fi), `src/lab/executors/dex/` (Jupiter, Uniswap/Aerodrome, PancakeSwap, STON.fi), `src/lab/strategies/meme/`, `tests/dex/`
**Волна:** 5
**Status:** ready

## Что должно заработать

Поток новых токенов и миграций: pump.fun через PumpPortal WS (бесплатные `subscribeNewToken/subscribeMigration`), EVM/BNB через DexScreener + сабграфы, TON через STON.fi; при 1000+ токенов/час все оцениваются, полная история хранится только по прошедшим первичный фильтр. Чек-лист честности до покупки: mint/freeze authority, доля топ-держателей, ликвидность, возраст, блок-лист — провал блокирует покупку. Исполнители DEX за протоколом `Executor` в `paper` и `live`: Jupiter (Solana), Uniswap/Aerodrome (Ethereum/Base), PancakeSwap (BNB), STON.fi (TON) с приоритетными fee и защитой от проскальзывания; неудачные транзакции (не прошла, MEV, застряла) пишутся с причиной и стоимостью попытки в издержки. Стратегии `meme-*` — только форвард (`can_backtest=False`), каждое решение в журнале до исхода; продажа лестницей (A05). Контрактный тест исполнителей расширен на DEX (фейковый RPC).

## Из брифа, дословно

> «торговля мемами»
> «покупка на ранней стадии»
> «Solana, Ethereum + Base, BNB Chain, TON»

## Разделы спецификации

Истории 65–71; Решения §5–§6, §10; `research-sources.md` §3–5.

## Критерии приёмки

- [ ] `feeds.dex.*` за `Feed.events(kind)`; PumpPortal WS с реконнектом; DexScreener с лимитом 300 rpm через `feeds_registry`
- [ ] Фильтр потока: оценка всех, хранение полной истории только прошедших; агрегаты по остальным; тест на 1000 событий
- [ ] `honesty_check(token) -> Checklist` с тестами на каждом пункте; провал → покупка запрещена
- [ ] `executors.dex.{jupiter,uniswap,pancake,stonfi}`: `paper` и `live`; slippage/priority из манифеста; отказ при превышении
- [ ] Четыре DEX-исполнителя зарегистрированы в `executors.registry`; контрактный тест проходит с фейковым RPC/HTTP
- [ ] Неудачные транзакции: причина + стоимость в `Costs.gas`; тест на «застряла → повтор с большим приоритетом»
- [ ] `meme-*` стратегии: `can_backtest=False`; ≥2 стратегии (ранний вход после миграции; вход по фильтру честности + объём); лестница продаж
- [ ] Записать сигнатуры и D## в `interfaces.md`
