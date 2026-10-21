#!/usr/bin/env bash
# Выкладка новой версии бумажного бота CryptosMX (crash-paper) без потери открытых позиций.
#
# Бот держит позиции только в памяти: перезапуск посреди сделки теряет её исход (зеркало —
# сверку К3, счёт ×1 — итог). Поэтому порядок (выведен на выкладке 20.10.2026):
#   1) проверки: место на диске, в worker не идёт замер (update.sh пересоздаёт ВСЕ сервисы);
#   2) git pull + сборка образа, пока бот работает (сборка — минуты, бот всё это время на связи);
#   3) ждать момента, когда у бота НЕТ открытых позиций (живут ≤ 7 мин), но не дольше
#      --wait-min минут; не дождались — выход без перезапуска;
#   4) deploy/update.sh (образ уже собран — пересоздание за секунды, ждёт healthy);
#   5) отчёт: версия кода в crash-paper, что бот восстановил из журнала, сколько позиций потерял.
#
# Запуск — с рабочей машины, отвязанно от ssh (сборка дольше, чем живёт сессия):
#   ssh vps-trader 'bash /opt/crypto-trading-lab/scripts/run-detached.sh crash-deploy \
#     "bash /opt/crypto-trading-lab/scripts/crash-paper-deploy.sh"'
#   ssh vps-trader 'bash /opt/crypto-trading-lab/scripts/run-detached.sh --wait crash-deploy'
# Ключи: --wait-min N (по умолчанию 15), --dry-run (только проверки и число открытых позиций).
#
# Коды выхода: 0 — выложено; 2 — не дождались нуля позиций; 3 — в worker идёт замер;
# 4 — мало места; прочее — ошибка шага (set -e).

# Всё тело — в функции: `git pull` ниже может переписать этот файл, а bash читает скрипт по ходу
# выполнения. Функция разбирается целиком до первого шага, и правка файла её уже не заденет.
main() {
  set -euo pipefail
  local wait_min=15 dry=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --wait-min) wait_min="$2"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      *) echo "неизвестный ключ: $1" >&2; return 1 ;;
    esac
  done

  cd /opt/crypto-trading-lab
  local dc=(docker compose -f deploy/docker-compose.yml)
  local journal=/app/data/crash-bounce-paper

  echo "[1/5] Проверки"
  local free_kb
  free_kb="$(df --output=avail -k / | tail -1 | tr -d ' ')"
  echo "  свободно на диске: $((free_kb / 1024 / 1024)) ГБ"
  if (( free_kb < 1024 * 1024 )); then
    echo "  меньше 1 ГБ — сначала docker builder prune -af" >&2
    return 4
  fi
  local extra
  extra="$(docker top crypto-trading-lab-worker-1 -o args 2>/dev/null | tail -n +2 \
           | grep -v "lab service worker" || true)"
  if [[ -n "$extra" ]]; then
    echo "  в worker идёт что-то кроме самого сервиса — пересоздание убило бы это:" >&2
    echo "$extra" | cut -c1-150 >&2
    return 3
  fi
  echo "  в worker замера нет"

  if (( dry )); then
    echo "  открытых позиций сейчас (код работающего бота):"
    "${dc[@]}" exec -T crash-paper /app/.venv/bin/python3 scripts/crash_bounce_paper.py \
      open --out "$journal" </dev/null || echo "  (у работающей версии нет команды open)"
    echo "--dry-run: дальше не иду"
    return 0
  fi

  echo "[2/5] Код и образ (бот пока работает)"
  local before
  before="$(git rev-parse --short HEAD)"
  git pull --ff-only
  echo "  $before → $(git rev-parse --short HEAD)"
  LAB_CODE_VERSION="$(git rev-parse HEAD)"
  export LAB_CODE_VERSION
  "${dc[@]}" build </dev/null

  echo "[3/5] Жду ноль открытых позиций (не дольше $wait_min мин)"
  # считает НОВЫЙ образ: у работающей версии команды `open` может не быть
  local n="" ok=0 i
  for ((i = 0; i < wait_min * 6; i++)); do
    n="$("${dc[@]}" run --rm --no-deps -T --entrypoint /app/.venv/bin/python3 crash-paper \
          scripts/crash_bounce_paper.py open --out "$journal" </dev/null 2>/dev/null \
          | tail -1 | tr -d '\r')"
    echo "  $(date -u +%H:%M:%S) UTC открыто: ${n:-?}"
    if [[ "$n" == "0" ]]; then ok=1; break; fi
    sleep 10
  done
  if (( ! ok )); then
    echo "НЕ ВЫЛОЖЕНО: за $wait_min мин так и не было момента без позиций (образ собран," \
         "контейнеры не тронуты). Повторить позже." >&2
    return 2
  fi

  echo "[4/5] Перезапуск: deploy/update.sh ($(date -u +%H:%M:%S) UTC)"
  bash deploy/update.sh </dev/null

  echo "[5/5] Бот после старта"
  sleep 20
  local ver
  ver="$(docker exec crypto-trading-lab-crash-paper-1 printenv LAB_CODE_VERSION 2>/dev/null || true)"
  echo "  версия кода в crash-paper: ${ver:0:12} (ожидалась ${LAB_CODE_VERSION:0:12})"
  "${dc[@]}" logs --tail 20 crash-paper </dev/null | grep -E "старт|восстановлены|×1:" || true
  if [[ "$ver" != "$LAB_CODE_VERSION" ]]; then
    echo "ВНИМАНИЕ: в crash-paper не та версия кода" >&2
    return 1
  fi
  echo "Готово. В журнал разбора: время перезапуска и «потеряно открытых позиций» из строки выше."
}

main "$@"; exit $?
