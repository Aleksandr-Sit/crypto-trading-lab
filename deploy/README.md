# Развёртывание

Четыре процесса (решение §1): `db` (Postgres 16), `worker`, `bot`, `web`, плюс одноразовый
`migrate`, который накатывает Alembic до `head` перед стартом сервисов.

```bash
cp .env.example .env            # заполнить хотя бы POSTGRES_PASSWORD
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps      # db/worker/bot/web — healthy
docker compose -f deploy/docker-compose.yml logs worker | head   # таблица площадок
```

- Данные базы живут в volume `lab_pgdata`: `down` / `up` их не теряет; `down -v` — теряет.
- Healthcheck сервисов — heartbeat-файл `/tmp/lab/<сервис>.heartbeat`, обновляется каждые 10 с.
- Веб слушает `${WEB_BIND}` (по умолчанию `127.0.0.1:8080`) — только localhost или за VPN.
- Конфиги `config/*.yaml` монтируются read-only: правка на хосте видна сервисам без пересборки.
- Резервные копии — volume `lab_backups` (`BACKUP_DIR` внутри контейнера: `/app/backups`).
