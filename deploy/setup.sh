#!/usr/bin/env bash
# Установка и первый запуск лаборатории на сервере — этап А из ЗАПУСК.md одной командой.
# Запускать из корня распакованного проекта:
#   bash deploy/setup.sh              # с вопросами (логин веба, Telegram, потолок капитала)
#   bash deploy/setup.sh -y           # ничего не спрашивать, всё по умолчанию
#
# Что делает: проверяет сервер, ставит Docker (если его нет), создаёт .env со случайными
# паролями, поднимает контейнеры, дожидается healthy и печатает памятку — логин, пароль,
# команду SSH-туннеля и следующий шаг. Повторный запуск безопасен: .env не перезаписывается.
set -euo pipefail

COMPOSE_FILE="deploy/docker-compose.yml"
DC=(docker compose -f "$COMPOSE_FILE")
INTERACTIVE=1
STEP="подготовка"
WEB_PASSWORD_GENERATED=""
HEALTH_OK=1

# -- вывод ------------------------------------------------------------------------

say()  { printf '%s\n' "$*"; }
step() { STEP="$1"; printf '\n%s\n' "$1"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }

die() {
  trap - ERR
  printf '\nНе получилось: %s\n' "$1" >&2
  [[ $# -gt 1 ]] && printf 'Что делать: %s\n' "$2" >&2
  exit 1
}

on_error() {
  local code=$?
  trap - ERR
  printf '\nСорвалось на шаге: %s (строка %s, код %s)\n' "$STEP" "${BASH_LINENO[0]}" "$code" >&2
  printf 'Что посмотреть:\n' >&2
  printf '  docker compose -f %s ps\n' "$COMPOSE_FILE" >&2
  printf '  docker compose -f %s logs --tail 50 worker\n' "$COMPOSE_FILE" >&2
  printf 'Когда разберёшься — запусти скрипт снова, он продолжит с того же места.\n' >&2
  exit "$code"
}
trap on_error ERR

usage() {
  cat <<'HELP'
Установка лаборатории на сервер (этап А). Запускать из корня распакованного проекта.

  bash deploy/setup.sh                 установка с вопросами
  bash deploy/setup.sh --non-interactive   всё по умолчанию, без вопросов
  bash deploy/setup.sh -y                  то же самое, короткая запись
  bash deploy/setup.sh --help              эта справка

Шаги: проверка сервера → Docker → файл настроек .env → запуск контейнеров →
проверка площадок → памятка (логин, пароль, SSH-туннель, что делать дальше).

Повторный запуск ничего не ломает: уже существующий .env остаётся как есть,
поднятые контейнеры просто обновляются.
HELP
}

# -- мелкие помощники --------------------------------------------------------------

# Случайная строка 24 символа: openssl, если он есть, иначе /dev/urandom.
gen_secret() {
  local value
  if command -v openssl >/dev/null 2>&1; then
    value="$(set +o pipefail; openssl rand -base64 48 | tr -d '/+=\n' | head -c 24)"
  else
    value="$(set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
  fi
  [[ ${#value} -ge 16 ]] || die "не удалось сгенерировать пароль" \
    "поставь openssl (apt-get install -y openssl) и повтори"
  printf '%s' "$value"
}

# Вопрос с ответом по умолчанию; в режиме без вопросов сразу отдаёт умолчание.
ask() {
  local prompt="$1" default="${2:-}" answer=""
  if [[ $INTERACTIVE -eq 0 ]]; then
    printf '%s' "$default"
    return 0
  fi
  read -r -p "$prompt" answer || answer=""
  printf '%s' "${answer:-$default}"
}

# Значение переменной из .env (без хвостового комментария и пробелов).
env_value() {
  awk -F= -v key="$1" '
    $1 == key {
      sub(/^[^=]*=/, "")
      sub(/[[:space:]]*#.*$/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "")
      print
      exit
    }' .env
}

# Записать значение переменной в .env (строка заменяется целиком).
set_env() {
  local name="$1" value="$2" escaped
  escaped="$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')"
  grep -q "^${name}=" .env \
    || die "в .env нет строки ${name}=" "возьми свежий .env.example из архива проекта"
  sed -i "s|^${name}=.*|${name}=${escaped}|" .env
}

# Состояние сервиса: healthy / starting / running / нет.
service_health() {
  local service="$1" cid
  cid="$("${DC[@]}" ps -q "$service" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    printf 'нет'
    return 0
  fi
  docker inspect --format \
    '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    "$cid" 2>/dev/null || printf 'нет'
}

# Внешний адрес сервера — из SSH-сессии, иначе из hostname, иначе плейсхолдер.
server_address() {
  local address=""
  if [[ -n "${SSH_CONNECTION:-}" ]]; then
    address="$(awk '{print $3}' <<<"$SSH_CONNECTION")"
  fi
  if [[ -z "$address" ]] && command -v hostname >/dev/null 2>&1; then
    address="$(hostname -I 2>/dev/null | awk '{print $1}')" || address=""
  fi
  printf '%s' "${address:-АДРЕС_СЕРВЕРА}"
}

# -- аргументы ---------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--non-interactive) INTERACTIVE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "непонятный аргумент: $1" "смотри bash deploy/setup.sh --help" ;;
  esac
  shift
done

if [[ $INTERACTIVE -eq 1 && ! -t 0 ]]; then
  INTERACTIVE=0
  say "Ввод не с терминала — работаю без вопросов, всё по умолчанию."
fi

say "Лаборатория: установка на сервер. Пять шагов, займёт 5-15 минут."

# -- [1/5] окружение ---------------------------------------------------------------

step "[1/5] Проверяю окружение…"

[[ "$(uname -s)" == "Linux" ]] \
  || die "скрипт рассчитан на Linux (Ubuntu на VPS), а здесь $(uname -s)" \
         "запусти его на сервере, а не на своём компьютере"

if [[ ! -f "$COMPOSE_FILE" || ! -f ".env.example" ]]; then
  target="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || true)"
  die "запускать надо из корня проекта — рядом должны лежать $COMPOSE_FILE и .env.example" \
      "перейди в папку проекта и повтори: cd ${target:-~/crypto-trading-lab} && bash deploy/setup.sh"
fi

command -v curl >/dev/null 2>&1 \
  || die "нет команды curl — без неё не поставить Docker" \
         "apt-get update && apt-get install -y curl, потом запусти скрипт снова"

if [[ "$(id -u)" -ne 0 ]] && ! id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  die "нужны права на Docker: ты не root и не состоишь в группе docker" \
      "запусти через sudo (sudo bash deploy/setup.sh) или выполни sudo usermod -aG docker $(id -un), перезайди по SSH и повтори"
fi

disk_free_kb="$(df -Pk . 2>/dev/null | awk 'NR==2 {print $4}')" || disk_free_kb=""
if [[ "$disk_free_kb" =~ ^[0-9]+$ && "$disk_free_kb" -lt 5242880 ]]; then
  warn "на диске свободно $((disk_free_kb / 1024 / 1024)) ГБ, а надо бы 5 ГБ и больше — свечи и база быстро займут место"
fi

mem_total_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || true)"
if [[ "$mem_total_kb" =~ ^[0-9]+$ && "$mem_total_kb" -lt 1572864 ]]; then
  warn "памяти на сервере $((mem_total_kb / 1024)) МБ, а надо бы 1.5 ГБ и больше — сервисы могут падать"
fi

say "Сервер подходит: Linux, curl есть, права на Docker есть."

# -- [2/5] Docker ------------------------------------------------------------------

step "[2/5] Проверяю Docker…"

if docker compose version >/dev/null 2>&1; then
  say "Docker уже стоит — ничего не меняю ($(docker compose version 2>/dev/null | head -1))."
else
  say "Docker не найден. Ставлю Docker, это займёт пару минут…"
  if ! curl -fsSL https://get.docker.com | sh; then
    die "установка Docker не удалась" \
        "проверь интернет на сервере (curl -I https://get.docker.com) и поставь вручную по инструкции docs.docker.com/engine/install/ubuntu/"
  fi
  docker compose version >/dev/null 2>&1 \
    || die "Docker поставился, но команда 'docker compose' не работает" \
           "перезайди по SSH и запусти скрипт снова; если не помогло — apt-get install -y docker-compose-plugin"
  say "Docker поставлен."
fi

# -- [3/5] настройки ---------------------------------------------------------------

step "[3/5] Готовлю файл настроек .env…"

if [[ -f .env ]]; then
  say ".env уже есть, оставляю как есть. Если надо поправить — nano .env, потом запусти скрипт снова."
else
  cp .env.example .env
  chmod 600 .env

  postgres_password="$(gen_secret)"
  web_password="$(gen_secret)"
  set_env POSTGRES_PASSWORD "$postgres_password"
  set_env WEB_PASSWORD "$web_password"
  WEB_PASSWORD_GENERATED="$web_password"
  say "Пароль базы и пароль веб-экрана придуманы случайно и записаны в .env."

  if [[ $INTERACTIVE -eq 1 ]]; then
    say ""
    say "Несколько вопросов. Если не знаешь ответ — жми Enter, поставлю значение по умолчанию."
  fi

  web_user="$(ask 'Логин для веб-экрана [admin]: ' 'admin')"
  set_env WEB_USER "$web_user"

  if [[ $INTERACTIVE -eq 1 ]]; then
    say ""
    say "Телеграм-бот (можно пропустить — тогда бот просто не включится, всё остальное работает)."
    say "  Токен: напиши @BotFather → /mybots → свой бот → API Token."
    say "  Твой числовой id: напиши @userinfobot, он пришлёт число в ответ."
  fi
  bot_token="$(ask 'Токен бота (Enter — пропустить): ' '')"
  [[ -n "$bot_token" ]] && set_env TELEGRAM_BOT_TOKEN "$bot_token"

  admin_id=""
  if [[ -n "$bot_token" ]]; then
    admin_id="$(ask 'Твой числовой id из @userinfobot (Enter — пропустить): ' '')"
    if [[ -n "$admin_id" && ! "$admin_id" =~ ^[0-9]+$ ]]; then
      warn "id должен быть числом, а введено «$admin_id» — пропускаю, впишешь потом в .env"
      admin_id=""
    fi
    [[ -n "$admin_id" ]] && set_env TELEGRAM_ADMIN_ID "$admin_id"
  fi
  if [[ -z "$bot_token" || -z "$admin_id" ]]; then
    say "Телеграм пока отключён — впишешь TELEGRAM_BOT_TOKEN и TELEGRAM_ADMIN_ID в .env позже."
  fi

  capital_cap="$(ask 'Потолок реального капитала, USD [1000]: ' '1000')"
  if [[ "$capital_cap" =~ ^[0-9]+$ ]]; then
    set_env REAL_CAPITAL_CAP "$capital_cap"
  else
    warn "«$capital_cap» — не число, ставлю 1000"
    set_env REAL_CAPITAL_CAP 1000
  fi

  say "Файл .env готов. Ключи бирж и источников допишешь туда позже — сейчас они не нужны."
fi

# Compose подставляет ${WEB_BIND}, ${DB_BIND}, ${POSTGRES_PASSWORD} из .env, который лежит
# РЯДОМ С COMPOSE-ФАЙЛОМ, то есть в deploy/, а наш .env — в корне проекта. Без этой ссылки
# порт веба и пароль базы молча берутся из умолчаний: веб встанет на 8080, даже если
# в .env указан другой порт, и займёт порт соседнего проекта.
ln -sfn ../.env deploy/.env
say "Настройки подключены к compose (deploy/.env → ../.env)."

# -- [4/5] запуск ------------------------------------------------------------------

step "[4/5] Собираю и запускаю сервисы (первый раз это несколько минут)…"

if ! "${DC[@]}" up -d --build; then
  die "контейнеры не поднялись" \
      "смотри причину: docker compose -f $COMPOSE_FILE logs --tail 50"
fi

say "Жду, пока сервисы отзовутся (до 2 минут)"
printf '  '
for _ in $(seq 1 24); do
  HEALTH_OK=1
  for service in worker bot web; do
    [[ "$(service_health "$service")" == "healthy" ]] || HEALTH_OK=0
  done
  [[ $HEALTH_OK -eq 1 ]] && break
  printf '.'
  sleep 5
done
printf '\n'

if [[ $HEALTH_OK -eq 1 ]]; then
  say "Все сервисы отвечают."
else
  warn "за 2 минуты не все сервисы стали healthy — возможно, ещё собираются, а возможно, есть ошибка"
  warn "посмотри: docker compose -f $COMPOSE_FILE logs --tail 50 worker"
fi

# -- [5/5] проверка ----------------------------------------------------------------

step "[5/5] Проверяю, что получилось…"

"${DC[@]}" ps || true

say ""
say "Площадки, видимые с этого сервера:"
if ! "${DC[@]}" exec -T worker uv run python -m lab ops status; then
  warn "не удалось спросить worker про площадки — он ещё поднимается или упал"
  warn "повтори позже: docker compose -f $COMPOSE_FILE exec worker uv run python -m lab ops status"
fi

jobs_line="$("${DC[@]}" logs worker 2>/dev/null | grep -o 'заданий [0-9]\+' | tail -1 || true)"
say ""
if [[ -n "$jobs_line" ]]; then
  say "Планировщик поднял: $jobs_line (бэкап, сверка, отчёт, поиск кандидатов, переизмерение)."
else
  say "Строку про задания планировщика пока не видно — она появится в логе worker при старте:"
  say "  docker compose -f $COMPOSE_FILE logs worker | grep заданий"
fi

# -- памятка -----------------------------------------------------------------------

web_user_final="$(env_value WEB_USER)"
[[ -n "$web_user_final" ]] || web_user_final="admin"
web_port_final="$(env_value WEB_BIND)"
web_port_final="${web_port_final##*:}"
[[ "$web_port_final" =~ ^[0-9]+$ ]] || web_port_final="8080"
ssh_user="$(id -un)"
address="$(server_address)"

cat <<MEMO

════════════════════════════════════════════════════════════════
 Готово. Что дальше
════════════════════════════════════════════════════════════════

1. Веб-экран. Наружу он не торчит, ходим через SSH-туннель.
   На СВОЁМ компьютере открой второе окно терминала и выполни:

     ssh -L 8080:127.0.0.1:${web_port_final} ${ssh_user}@${address}

   Не закрывая его, открой в браузере:  http://127.0.0.1:8080

   Логин:  ${web_user_final}
MEMO

if [[ -n "$WEB_PASSWORD_GENERATED" ]]; then
  cat <<MEMO
   Пароль: ${WEB_PASSWORD_GENERATED}

   Пароль показан один раз — запиши его. Он же лежит в .env (строка WEB_PASSWORD).
MEMO
else
  cat <<'MEMO'
   Пароль: он в файле .env, строка WEB_PASSWORD — посмотри там.
MEMO
fi

cat <<MEMO

2. Телеграм. Напиши своему боту /status — он ответит состоянием системы.
   Если бот молчит или ты пропустил вопросы про телеграм: впиши
   TELEGRAM_BOT_TOKEN и TELEGRAM_ADMIN_ID в .env и выполни
   docker compose -f ${COMPOSE_FILE} up -d

3. Следующий шаг — скачать историю свечей (ключи для этого не нужны,
   занимает время, прерывать и продолжать можно):

     docker compose -f ${COMPOSE_FILE} exec worker \\
       uv run python -m lab data backfill --venue bybit --symbols BTC/USDT:USDT --tf 1h --days 365

   Дальше по порядку — этап Б в ЗАПУСК.md.

Полезное:
  docker compose -f ${COMPOSE_FILE} ps                 кто жив
  docker compose -f ${COMPOSE_FILE} logs -f worker     что происходит
MEMO

if [[ $HEALTH_OK -ne 1 ]]; then
  say ""
  warn "не все сервисы поднялись — система записана и настроена, но проверь логи worker"
  exit 1
fi

exit 0
