#!/usr/bin/env bash
# Восстановление из резервной копии (R25.3). Использование:
#   scripts/restore.sh backups/lab-2026-09-07.tar.gz [DATABASE_URL]
#
# Что делает: проверяет архив, разворачивает его во временный каталог, проверяет
# доступность базы (pg_isready), восстанавливает дамп в ПУСТУЮ или существующую базу
# (psql -v ON_ERROR_STOP=1) и кладёт конфиги рядом с архивом (config.restored/),
# чтобы не затирать рабочие вслепую.
set -euo pipefail

ARCHIVE="${1:-}"
DB_URL="${2:-${DATABASE_URL:-}}"

if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "Укажи архив: scripts/restore.sh backups/lab-YYYY-MM-DD.tar.gz [DATABASE_URL]" >&2
  exit 2
fi
if [[ -z "$DB_URL" ]]; then
  echo "Не задан DATABASE_URL (аргумент или переменная окружения)" >&2
  exit 2
fi

# SQLAlchemy-URL (postgresql+psycopg://) → строка для psql
PG_URL="${DB_URL/postgresql+psycopg:\/\//postgresql://}"
PG_URL="${PG_URL/postgresql+psycopg2:\/\//postgresql://}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "1/4 Проверяю архив $ARCHIVE"
tar -tzf "$ARCHIVE" | grep -q '^db.sql$' || { echo "В архиве нет db.sql" >&2; exit 3; }
tar -xzf "$ARCHIVE" -C "$TMP"

echo "2/4 Проверяю базу"
if ! pg_isready -d "$PG_URL" >/dev/null 2>&1; then
  echo "База недоступна по $PG_URL — подними её (docker compose up -d db) и повтори" >&2
  exit 4
fi

echo "3/4 Восстанавливаю дамп (psql)"
psql "$PG_URL" -v ON_ERROR_STOP=1 -q -f "$TMP/db.sql"

echo "4/4 Конфиги из архива"
if [[ -d "$TMP/config" ]]; then
  DEST="$(dirname "$ARCHIVE")/config.restored"
  rm -rf "$DEST"; mkdir -p "$DEST"
  cp -a "$TMP/config/." "$DEST/"
  echo "Конфиги распакованы в $DEST — сверь с рабочим config/ и перенеси нужное вручную"
fi

echo "Готово. Проверь: uv run alembic current  и  uv run python -m lab strategy list"
