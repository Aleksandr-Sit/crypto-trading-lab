# Развёртывание на VPS

Четыре процесса: `db` (Postgres 16), `worker` (планировщик, фиды, исполнение, сверка),
`bot` (Telegram), `web` (FastAPI за HTTP Basic). Всё поднимается одной командой,
состояние живёт в томах Docker и переживает перезапуск (R28i, R32i).

## 1. Что нужно на сервере

- Docker 24+ с плагином `compose` (`docker compose version`).
- 2 ГБ памяти и 20 ГБ диска — хватает на базу, Parquet-свечи и месяц резервных копий.
- Открытых портов наружу не требуется: веб слушает `127.0.0.1` и ходят на него
  через SSH-туннель (`ssh -L 8080:127.0.0.1:8080 user@vps`) или ваш reverse-proxy.

```bash
git clone <репозиторий> crypto-trading-lab && cd crypto-trading-lab
cp .env.example .env && nano .env      # заполнить секреты
```

## 2. `.env`

Имена — строго из `.env.example`; в коде и логах ключей нет, система показывает только
«заполнено / пусто» (`uv run python -m lab venues`). Обязательный минимум:

| Переменная | Зачем |
|---|---|
| `POSTGRES_PASSWORD` | пароль пользователя `lab` в контейнере базы |
| `DATABASE_URL` | можно не задавать — compose соберёт `postgresql+psycopg://lab:…@db:5432/lab` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID` | бот и единственный админ, от кого принимаются команды |
| `WEB_USER`, `WEB_PASSWORD` | HTTP Basic веб-экрана; без них `web` не стартует |
| `WEB_BIND` | адрес:порт веба, по умолчанию `127.0.0.1:8080` |
| `BACKUP_DIR` | каталог копий внутри контейнера (том `lab_backups`, по умолчанию `/app/backups`) |
| `BUDGET_MONTH_USD` | лимит на платные источники, по умолчанию 50 |
| `REAL_CAPITAL_CAP` | потолок реального капитала первой фазы |
| ключи площадок и источников | по мере подключения; **ключ с правом вывода средств отклоняется при старте** |

## 3. Первый запуск

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps          # healthcheck: healthy у worker/bot/web
docker compose -f deploy/docker-compose.yml logs -f worker
```

Что происходит: `migrate` накатывает миграции (`alembic upgrade head`) и завершается,
затем стартуют `worker`, `bot`, `web` с `restart: unless-stopped`. Worker при старте
печатает таблицу доступности площадок с текущего IP (гео-блок виден сразу), сверяет
активные live-ордера с площадками и поднимает планировщик.

Проверить руками:

```bash
docker compose -f deploy/docker-compose.yml exec worker uv run python -m lab ops status   # площадки
docker compose -f deploy/docker-compose.yml exec worker uv run python -m lab ops feeds    # квоты и бюджет
```

В Telegram — `/status`; веб — `http://127.0.0.1:8080/` (логин из `WEB_USER`).

## 4. Обновление

```bash
git pull && docker compose -f deploy/docker-compose.yml up -d --build
```

Данные не теряются: база и Parquet лежат в томах `lab_pgdata` / `lab_data`, миграции
накатываются автоматически сервисом `migrate` до старта остальных (он в `depends_on`
с `service_completed_successfully`).

## 5. Резервные копии и восстановление

Каждый день в 03:00 (Europe/Samara) worker кладёт в `BACKUP_DIR` архив
`lab-ГГГГ-ММ-ДД.tar.gz` — дамп базы (`db.sql`) плюс каталог `config/`; копии старше
14 суток удаляются. Разовая копия:

```bash
docker compose -f deploy/docker-compose.yml exec worker uv run python -m lab ops backup
docker compose -f deploy/docker-compose.yml cp worker:/app/backups ./backups   # забрать с сервера
```

Восстановление (проверяет архив и доступность базы, потом разворачивает дамп):

```bash
docker compose -f deploy/docker-compose.yml up -d db
scripts/restore.sh backups/lab-2026-09-07.tar.gz "postgresql://lab:ПАРОЛЬ@localhost:5432/lab"
docker compose -f deploy/docker-compose.yml up -d
```

Конфиги из архива распаковываются рядом в `config.restored/` — рабочий `config/`
скрипт не затирает, перенесите нужное руками.

## 6. Наблюдение

- Логи: `docker compose -f deploy/docker-compose.yml logs -f worker|bot|web`.
- Watchdog: каждый сервис пишет heartbeat в базу; молчание 5 минут → карточка `alert`
  в Telegram.
- Сверка журнала с площадками — ежедневно в 04:00, расхождение → `alert`.
- Бюджетомер источников предупреждает при 80% и 100% от `BUDGET_MONTH_USD`.

## 7. Перезагрузка конфигов без перезапуска

```bash
docker compose -f deploy/docker-compose.yml exec worker uv run python -m lab ops reload
# или сигналом:
docker compose -f deploy/docker-compose.yml kill -s HUP worker
```

Изменение пишется в таблицу `config_changes` (кто, когда, какой файл). Сломанный конфиг
не применяется — работает предыдущий, ошибка видна в логе.

## 8. Если что-то не поднялось

| Симптом | Что смотреть |
|---|---|
| `web` не стартует | не заданы `WEB_USER`/`WEB_PASSWORD` |
| `bot` пишет «TELEGRAM_… не заданы» | пустые `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ADMIN_ID` |
| площадка `гео-блок` в `ops status` | биржа закрыта для IP сервера — ветка живёт в режиме замера |
| `migrate` упал | `docker compose ... run --rm migrate uv run alembic upgrade head` и смотреть вывод |
