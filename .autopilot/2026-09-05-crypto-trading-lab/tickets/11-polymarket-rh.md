# 11 — Polymarket и Robinhood

**Требования:** G02, G02.1, G02.2, G01, G01.1
**Blocked by:** 04, 07
**Зона:** `src/lab/feeds/polymarket/`, `src/lab/executors/polymarket/`, `src/lab/strategies/prediction/`, `src/lab/feeds/robinhood/`, `src/lab/executors/robinhood/`, `tests/polymarket/`, `tests/robinhood/`
**Волна:** 4
**Status:** ready

## Что должно заработать

Polymarket: Gamma/CLOB/Data API за `Feed` (рынки, история цен, лидерборд `/v1/leaderboard`, позиции кошельков); исполнитель через `py-clob-client` в `paper`/`live`; стратегия `pm-copy-*` копирует позиции топ-кошельков; метрики ветки `prediction` — Brier, доходность к резолюции — приведены к общим P&L/DD; при недоступности торговли с IP/аккаунта — ветка в режиме «только замер» с пометкой. Robinhood: официальный Crypto API (ключ + Ed25519) за `Feed`/`Executor` для крипты; при недоступности для аккаунта — ветка `rh` в режиме сигналов с пометкой в дашборде; акции — только сигналы (стратегии из тикета 07, `asset_class: stock`).

## Из брифа, дословно

> «так же рассмотрим robin hood, Polymarket»
> «Оба в первой фазе»

## Разделы спецификации

Истории 82–86; Решения §5–§6; `research-sources.md` §7–8.

## Критерии приёмки

- [ ] `feeds.polymarket`: рынки, `prices-history`, лидерборд, позиции; лимиты Data API через `feeds_registry`
- [ ] `executors.polymarket` за `Executor` (`paper`/`live`); зарегистрирован в `executors.registry`, контрактный тест проходит
- [ ] `pm-copy-*` стратегия; `brier`, `resolution_return` в метриках ветки; общие P&L/DD считаются
- [ ] Проверка доступности торговли при старте → `read_only` флаг ветки; отражается в `/status`
- [ ] `feeds.robinhood` + `executors.robinhood` (Crypto API, Ed25519 подпись); недоступность → режим сигналов + пометка
- [ ] Тесты с фейковыми HTTP-ответами по документации; живые — за флагом
- [ ] Записать сигнатуры и D## в `interfaces.md`
