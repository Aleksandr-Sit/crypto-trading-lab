#!/usr/bin/env bash
# Обновление лаборатории на сервере одной командой:
#   cd /opt/crypto-trading-lab && bash deploy/update.sh
#
# Делает то же, что раньше делалось руками (git pull + up -d --build), плюс две вещи,
# которые руками забываются: подставляет версию кода в образ (иначе снимок замера
# получает code_version=unknown и замер невоспроизводим) и дожидается healthy,
# а не рапортует об успехе сразу после запуска.
set -euo pipefail

COMPOSE_FILE="deploy/docker-compose.yml"
DC=(docker compose -f "$COMPOSE_FILE")

[[ -f "$COMPOSE_FILE" ]] || {
  echo "Запускать из корня проекта: cd /opt/crypto-trading-lab && bash deploy/update.sh" >&2
  exit 1
}

echo "[1/4] Забираю код из GitHub…"
before="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
git pull --ff-only
after="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
if [[ "$before" == "$after" ]]; then
  echo "  без изменений ($after) — пересоберу на всякий случай"
else
  echo "  $before → $after"
fi

echo "[2/4] Собираю образ (версия кода уедет в снимок замера)…"
LAB_CODE_VERSION="$(git rev-parse HEAD 2>/dev/null || true)"
export LAB_CODE_VERSION
"${DC[@]}" up -d --build

echo "[3/4] Жду, пока сервисы отзовутся (до 2 минут)"
ok=0
for _ in $(seq 1 24); do
  ok=1
  for service in worker bot web; do
    cid="$("${DC[@]}" ps -q "$service" 2>/dev/null || true)"
    state="нет"
    [[ -n "$cid" ]] && state="$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo нет)"
    [[ "$state" == "healthy" ]] || ok=0
  done
  [[ $ok -eq 1 ]] && break
  printf '.'
  sleep 5
done
printf '\n'

echo "[4/4] Что получилось:"
"${DC[@]}" ps --format "  {{.Service}}\t{{.Status}}"

if [[ $ok -ne 1 ]]; then
  echo "" >&2
  echo "ВНИМАНИЕ: не все сервисы стали healthy за 2 минуты." >&2
  echo "Смотреть: docker compose -f $COMPOSE_FILE logs --tail 50 worker" >&2
  exit 1
fi
echo "Готово. Версия кода в образе: ${LAB_CODE_VERSION:0:12}"
