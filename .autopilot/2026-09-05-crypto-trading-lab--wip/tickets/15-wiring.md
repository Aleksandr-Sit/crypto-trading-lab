# 15 — Связка: замер запускается сам; .env читается как обещано

**Требования:** R12, R11, R14, R10, R13, R32i
**Blocked by:** 14
**Зона:** `src/lab/ops/worker.py`, `src/lab/ops/jobs/__init__.py`, `src/lab/bot/core.py`, `src/lab/cli.py`, `src/lab/config/env.py`, `.env.example`, `README.md`, `deploy/README.md`, `src/lab/discovery/decisions.py` (только вызов measure), `tests/ops/`, `tests/config/`
**Волна:** 7
**Status:** ready

## Что должно заработать

Слепая приёмка (G4) нашла: движок замера рабочий, но **ни один компонент его не вызывает** — `remeasure` не регистрируется, бот при «В замер» не меряет, CLI-команды замера нет. Центральное требование брифа в запущенном продукте не исполняется. И второе: сценарий из README (`cp .env.example .env`) ломает систему — хвостовые комментарии попадают в значения.

## Из брифа, дословно

> «будем все измерять и исследовать, ничего не отвергаем и не подтверждаем на теории, только замерами и тестами»
> «периодическим обновлением и поиском новых стратегий»

## Критерии приёмки

- [ ] `Worker` собирает `measure` (обёртку над `core.measure.run`, которая сама достаёт стратегию из реестра, свечи из `CandleStore`/фида и бенчмарк BTC) и передаёт его в `discovery_jobs(measure=...)`; `lab service worker --once` печатает 12 заданий, включая `remeasure`
- [ ] Бот при «В замер» реально меряет: `candidate_hook` получает `measure=`, `DecisionResult.measured` становится `True`, в карточке — результат
- [ ] CLI: `lab measure run <strategy_id> [--mode backtest|paper|forward] [--days N]` и `lab measure show <strategy_id>` — вручную запустить и посмотреть; тест на обе
- [ ] Очередь кандидатов сортируется по `measure_cost` (В6 «по цене замера»), а не по id
- [ ] `load_dotenv` срезает хвостовой комментарий вне кавычек (`FOO=bar # комментарий` → `bar`); тест с файлом ровно того вида, что лежит в `.env.example`
- [ ] `lab venues` на скопированном без правок `.env.example` показывает все площадки «только данные (ключ пуст)», а не «подключена»
- [ ] `lab service web` читает `WEB_USER`/`WEB_PASSWORD` из `.env` (сейчас `credentials_from_env(None)` идёт мимо); тест
- [ ] `lab service worker --once` на скопированном без правок `.env.example` не падает (пустой `TELEGRAM_BOT_TOKEN` → бот отключён, а не `TokenValidationError`)
- [ ] `.env.example`: `DATABASE_URL` по умолчанию рабочий для `docker compose` и с комментарием, как заменить для локального запуска; README и `deploy/README.md` — сценарий, который проходит без правок
- [ ] `lab venues` и `lab ops status` не расходятся в оценке ключей Polymarket
- [ ] `uv run pytest -q` зелёный (было 505 passed, 29 skipped), `ruff` чист
