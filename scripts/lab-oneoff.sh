#!/bin/bash
# Одноразовый прогон скрипта из scripts/ на сервере — С РАБОЧЕЙ МАШИНЫ (Git Bash).
#
# Зачем: 26.09.2026 одна и та же длинная команда набиралась руками четыре раза за сессию —
# scp на сервер, одноразовый контейнер, линт, пробный запуск, — и в каждом наборе можно было
# заново наступить на один из трёх уроков ниже. Теперь это одна строка.
#
# Как пользоваться (из корня репозитория):
#   bash scripts/lab-oneoff.sh [--lint | --lint-only] [--trades-sample N] \
#        <скрипт.py> [ещё .py…] [-- <аргументы запуска>] [-- <аргументы следующего запуска>…]
#
#   bash scripts/lab-oneoff.sh --lint-only scripts/leverage_persist.py
#   bash scripts/lab-oneoff.sh --lint --trades-sample 5 scripts/crowd_flow.py \
#        -- --stage scan --trades /lint/trades --root /lint/root \
#        -- --stage check --root /lint/root
#
# Что делает:
# * копирует файлы в vps-trader:/tmp/lint/ ВМЕСТЕ с модулями из scripts/, которые они
#   импортируют (copytrade_screen.py и т.п.), — чтобы работали локальные, ещё не
#   закоммиченные версии, а не те, что лежат в /opt/crypto-trading-lab. Старые .py из
#   /tmp/lint перед этим удаляются; каталоги с данными (trades/, root/) не трогаются;
# * --lint: ruff по всем отправленным файлам с конфигом репозитория; нашёл ошибки —
#   запуска нет. --lint-only: только линт;
# * каждый блок после `--` — отдельный запуск ПЕРВОГО файла с этими аргументами. Блоки идут
#   по очереди в одном контейнере и останавливаются на первой ошибке — так стадии
#   scan → classify → flow → check проходят одной командой. Без `--` — запуск без аргументов;
# * --trades-sample N: в /tmp/lint/trades кладутся N последних ЗАКРЫТЫХ часов потока wake
#   (текущий час пишется несжатым .jsonl и не берётся), внутри контейнера это /lint/trades.
#
# Три урока, которые здесь зашиты (подробно — CLAUDE.md):
# * файлы монтируются ОТДЕЛЬНЫМ каталогом /lint, а не внутрь /src: файл, смонтированный
#   внутрь репозитория, оставляет на хосте пустую заглушку, и следующий git pull отказывает;
# * -T и </dev/null у docker compose: иначе он читает stdin и обрывает всё, что идёт следом;
# * --no-deps: база уже работает, поднимать зависимости не нужно;
# * LAB_DATA_ROOT=/app/data: иначе при -w /src хранилище свечей пусто, и замер тихо идёт
#   за данными на биржу (там лимиты и сбои) вместо тома.
#
# Долгие прогоны (дольше нескольких минут) — с ключом --detached <имя>: ssh рвётся на
# многоминутных операциях и убивает всё, что запущено в переднем плане.
#   bash scripts/lab-oneoff.sh --lint --detached yt1h scripts/youtube_check.py -- <аргументы> …
#   bash scripts/lab-oneoff.sh --wait yt1h     # дождаться и напечатать лог целиком
#   bash scripts/lab-oneoff.sh --clean yt1h    # убрать каталог, лог и обёртку на сервере
# Код кладётся в СВОЙ каталог /root/oneoff-<имя>, а не в /tmp/lint: тот перетирается
# каждым следующим вызовом, и отвязанная задача получила бы чужой скрипт посреди работы.
# Запуск — через scripts/run-detached.sh сервера, лог /root/<имя>.log. Две задачи
# с разными именами можно пустить рядом; больше двух — нет: на сервере живёт crypto-trader.
# 29.09.2026 это собиралось руками (свой каталог, обёртка, run-detached) — отсюда ключ.

set -u

HOST=vps-trader
REPO=/opt/crypto-trading-lab
LINT_DIR=/tmp/lint
WAKE_TRADES=/opt/wake/data/trades
HERE="$(cd "$(dirname "$0")" && pwd)"

# Кавычки для sh: a'b → 'a'\''b'. Команда проходит ДВА разбора — оболочкой сервера и sh -c
# в контейнере, — поэтому аргументы с пробелами, $ и кириллицей заворачиваются дважды.
q() { local r="'\\''"; printf "'%s'" "${1//\'/"$r"}"; }

LINT=0
LINT_ONLY=0
SAMPLE=0
DETACHED=""
ACTION=""
FILES=()
RUNS=()
CUR=""
IN_ARGS=0

while [ $# -gt 0 ]; do
    if [ "$IN_ARGS" = 1 ]; then
        if [ "$1" = "--" ]; then RUNS+=("$CUR"); CUR=""; else CUR+=" $(q "$1")"; fi
        shift
        continue
    fi
    case "$1" in
        --lint) LINT=1 ;;
        --lint-only) LINT=1; LINT_ONLY=1 ;;
        --trades-sample) SAMPLE="${2:?после --trades-sample нужно число часов}"; shift ;;
        --detached) DETACHED="${2:?после --detached нужно имя задачи}"; shift ;;
        --wait|--clean) ACTION="$1"; DETACHED="${2:?после $1 нужно имя задачи}"; shift ;;
        --) IN_ARGS=1 ;;
        -h|--help) sed -n '2,/^$/p' "$0"; exit 0 ;;
        -*) echo "неизвестный ключ: $1 (аргументы скрипта — после --)" >&2; exit 2 ;;
        *) FILES+=("$1") ;;
    esac
    shift
done
[ "$IN_ARGS" = 1 ] && RUNS+=("$CUR")
[ ${#RUNS[@]} -eq 0 ] && RUNS=("")

# Имя идёт в пути /root/… и в команду сервера: только безопасные символы.
case "$DETACHED" in *[!A-Za-z0-9_-]*) echo "имя задачи: латиница, цифры, _ и -" >&2; exit 2 ;; esac
if [ -n "$DETACHED" ]; then LINT_DIR="/root/oneoff-$DETACHED"; fi
if [ "$ACTION" = "--wait" ]; then
    exec ssh -n "$HOST" "bash $REPO/scripts/run-detached.sh --wait $DETACHED"
fi
if [ "$ACTION" = "--clean" ]; then
    exec ssh -n "$HOST" "rm -rf $LINT_DIR /root/$DETACHED.log /root/$DETACHED.sh && echo 'убрано: $DETACHED'"
fi
[ ${#FILES[@]} -gt 0 ] || { sed -n '8,14p' "$0" >&2; exit 2; }
case "$SAMPLE" in *[!0-9]*) echo "--trades-sample: нужно число, а не «$SAMPLE»" >&2; exit 2 ;; esac

# Файл берётся как указан, а если его нет — из scripts/ по имени.
PATHS=()
NAMES=()
add() {
    local name
    name="$(basename "$1")"
    case " ${NAMES[*]} " in *" $name "*) return ;; esac
    PATHS+=("$1")
    NAMES+=("$name")
}
for f in "${FILES[@]}"; do
    if [ -f "$f" ]; then add "$f"
    elif [ -f "$HERE/$(basename "$f")" ]; then add "$HERE/$(basename "$f")"
    else echo "нет файла: $f" >&2; exit 2
    fi
done

# Локальные модули, которые импортируются через sys.path.insert(… parent): без них на
# сервере подхватились бы старые копии из репозитория — или не нашлось бы ничего.
i=0
while [ $i -lt ${#PATHS[@]} ]; do
    for m in $(grep -oE '^(from|import) [A-Za-z_][A-Za-z0-9_]*' "${PATHS[$i]}" | cut -d' ' -f2); do
        [ -f "$HERE/$m.py" ] && add "$HERE/$m.py"
    done
    i=$((i + 1))
done
MAIN="${NAMES[0]}"
echo "на сервер: ${NAMES[*]}"

if [ -n "$DETACHED" ]; then
    # Живая задача с тем же именем: её код лежит в этом же каталоге, перетирать нельзя.
    ssh -n "$HOST" "[ ! -f /root/$DETACHED.log ] || grep -q 'ГОТОВО код=' /root/$DETACHED.log" \
        || { echo "задача $DETACHED ещё идёт — дождись (--wait) или возьми другое имя" >&2; exit 1; }
fi

PREP="mkdir -p $LINT_DIR && rm -rf $LINT_DIR/*.py $LINT_DIR/__pycache__"
if [ "$SAMPLE" -gt 0 ]; then
    PREP+=" && rm -rf $LINT_DIR/trades && mkdir -p $LINT_DIR/trades"
    PREP+=" && ls $WAKE_TRADES/*.jsonl.gz | sort | tail -n $SAMPLE | xargs -r cp -t $LINT_DIR/trades/"
    PREP+=" && echo \"срез wake: \$(ls $LINT_DIR/trades | sed -n '1p;\$p' | xargs)\""
fi
ssh -n "$HOST" "$PREP" || exit 1
scp -q "${PATHS[@]}" "$HOST:$LINT_DIR/" || exit 1

COMPOSE="cd $REPO && docker compose -f deploy/docker-compose.yml run --rm --no-deps -T"
COMPOSE+=" -v $REPO:/src -v $LINT_DIR:/lint -w /src -e PYTHONUNBUFFERED=1"
# Хранилище — явным путём: по умолчанию оно ОТНОСИТЕЛЬНОЕ (`data`) и у воркера находится
# только потому, что его каталог /app. При -w /src замер искал свечи в /src/data, не находил
# и молча качал их с биржи: 27.09.2026 один замер так прошёл (снимок 130), два упали на
# лимите запросов Bybit — и ни один не сказал, что хранилище не найдено.
COMPOSE+=" -e LAB_DATA_ROOT=/app/data"

# docker compose пишет «Container … Creating/Created» на каждый запуск — это шум.
remote() {
    ssh -n "$HOST" "$COMPOSE $1 --entrypoint sh worker -c $(q "$2") </dev/null" 2>&1 \
        | grep --line-buffered -v '^ *Container '
    return "${PIPESTATUS[0]}"
}

if [ "$LINT" = 1 ]; then
    TARGETS=""
    for n in "${NAMES[@]}"; do TARGETS+=" /lint/$n"; done
    # Окружение с ruff живёт на хосте (/tmp/lint-venv), а не внутри контейнера: с нуля
    # uv sync занимает ~50 с, а повторный — секунды. Не в /tmp/lint, чтобы не путать с
    # подложенными файлами.
    remote "-v /tmp/lint-venv:/venv -e UV_NO_SYNC=0 -e UV_PROJECT_ENVIRONMENT=/venv" \
        "uv sync --group dev -q && /venv/bin/ruff check --no-cache \
--config /src/pyproject.toml$TARGETS" || { echo "линт не прошёл — запуска нет" >&2; exit 1; }
    [ "$LINT_ONLY" = 1 ] && exit 0
fi

CMD=""
for r in "${RUNS[@]}"; do
    [ -n "$CMD" ] && CMD+=" && "
    [ ${#RUNS[@]} -gt 1 ] && CMD+="echo '>>> $MAIN'$r && "
    CMD+="/app/.venv/bin/python /lint/$MAIN$r"
done
if [ -z "$DETACHED" ]; then
    remote "" "$CMD"
    exit
fi

# Отвязанно: та же команда, что у remote(), но в файле-обёртке в каталоге задачи. Файл
# сервер разбирает так же, как строку ssh, — кавычки q() работают без изменений. Код
# возврата — python, а не grep: run-detached пишет в лог «ГОТОВО код=» последней команды.
RUN="$(mktemp)"
{
    echo '#!/bin/bash'
    echo "$COMPOSE --entrypoint sh worker -c $(q "$CMD") </dev/null 2>&1 | grep -v '^ *Container '"
    echo 'exit "${PIPESTATUS[0]}"'
} > "$RUN"
scp -q "$RUN" "$HOST:$LINT_DIR/run.sh" || { rm -f "$RUN"; exit 1; }
rm -f "$RUN"
ssh -n "$HOST" "bash $REPO/scripts/run-detached.sh $DETACHED 'bash $LINT_DIR/run.sh'" || exit 1
echo "ждать:  bash scripts/lab-oneoff.sh --wait $DETACHED"
echo "убрать: bash scripts/lab-oneoff.sh --clean $DETACHED"
