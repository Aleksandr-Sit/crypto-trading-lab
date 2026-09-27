#!/bin/bash
# Тесты и линт ЛОКАЛЬНОЙ рабочей копии на сервере — С РАБОЧЕЙ МАШИНЫ (Git Bash).
#
# Зачем: на рабочей машине нет ни uv, ни окружения проекта, а /opt/crypto-trading-lab на
# сервере — последний git pull, то есть НЕ та правка, которую надо проверить. 27.09.2026
# это делалось руками в шесть команд: упаковать дерево, отправить, написать обёртку,
# запустить отвязанно, дождаться, убрать за собой. Теперь это одна строка.
#
# Как пользоваться (из корня репозитория):
#   bash scripts/lab-test.sh                                   # весь набор + ruff
#   bash scripts/lab-test.sh tests/ops/test_place_signal.py -x # аргументы — в pytest
#
# Код выхода: как у pytest (0 — зелёный, 1 — падения, 4 — ошибка вызова, 5 — ничего не
# собрано); свои коды — выше pytest: 10 — не поставились зависимости, 11 — тесты прошли,
# ruff нет, 12 — база lab_test недоступна.
#
# Что делает:
# * отправляет в vps-trader:/root/lab-test-src файлы под git ПЛЮС неотслеживаемые, но не
#   игнорируемые (новый тест, ещё не добавленный в индекс), — `git ls-files -co
#   --exclude-standard`. Файлы .env* (кроме .env.example) не уезжают никогда: если такой
#   вдруг оказался в списке, скрипт отказывается работать;
# * гоняет pytest и ruff в одноразовом контейнере worker с этой копией в /src. Окружение
#   живёт на хосте (/tmp/lab-test-venv, ~680 МБ): повторный uv sync — 2 с, контейнер
#   целиком — 8 с. Прогон одного файла занимает ~40 с, и больше половины — сеть: каждое
#   ssh-подключение с рабочей машины стоит ~2 с, а их здесь с десяток плюс опрос итога;
# * ждёт итога короткими ssh-проверками и печатает лог, маскируя пароль в адресах баз;
# * убирает за собой: копию, обёртку задачи и лог (через shred — в нём мог оказаться
#   секрет из упавшего assert).
#
# Уроки, которые здесь зашиты (подробно — CLAUDE.md):
# * без TEST_DATABASE_URL молча пропускаются все тесты с базой, а прогон выглядит зелёным.
#   Адрес собирается подстановкой из DATABASE_URL самого контейнера, и до pytest
#   проверяется, что база отвечает;
# * задача идёт через run-detached.sh: ssh рвётся на многоминутных операциях;
# * -T и </dev/null у docker compose: иначе он читает stdin и обрывает всё, что идёт следом.

set -u

HOST=vps-trader
REPO=/opt/crypto-trading-lab
SRC=/root/lab-test-src
VENV=/tmp/lab-test-venv
JOB=labtest
LOG=/root/$JOB.log
WAIT_S=5
WAIT_MAX=360  # × WAIT_S = 30 минут

case "${1:-}" in -h|--help) sed -n '2,/^$/p' "$0"; exit 0 ;; esac

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)" || exit 2

# Кавычки для sh: a'b → 'a'\''b'.
q() { local r="'\\''"; printf "'%s'" "${1//\'/"$r"}"; }

SECRETS="$(git ls-files -co --exclude-standard | grep -E '(^|/)\.env(\.[^/]*)?$' \
    | grep -vE '(^|/)\.env\.example$')"
if [ -n "$SECRETS" ]; then
    echo "ОТКАЗ: в список на отправку попали файлы окружения:" >&2
    echo "$SECRETS" >&2
    echo "Добавь их в .gitignore — секреты на сервер копией не уезжают." >&2
    exit 2
fi

ssh -n "$HOST" "[ -f $LOG ] && ! grep -q 'ГОТОВО код=' $LOG" && {
    echo "ОТКАЗ: прошлый прогон ещё идёт ($HOST:$LOG). Дождись или удали лог." >&2
    exit 2
}

# Без аргументов — весь набор с -q; с аргументами -q не добавляется: он гасит их -v.
ARGS=""
for a in "$@"; do ARGS+=" $(q "$a")"; done
[ -n "$ARGS" ] || ARGS=" -q"

echo "копия рабочего дерева → $HOST:$SRC"
git ls-files -co --exclude-standard -z | tar --null -T - -czf - \
    | ssh "$HOST" "rm -rf $SRC && mkdir -p $SRC && tar -xzf - -C $SRC" || exit 1

# Обёртка внутри контейнера. DATABASE_URL раскрывается ТАМ, а не здесь: пароль не попадает
# ни в командную строку, ни в историю оболочки.
{
    cat <<'SH'
#!/bin/sh
export TEST_DATABASE_URL="${DATABASE_URL%/lab}/lab_test"
uv sync --group dev -q || exit 10
/venv/bin/python -c 'import os, sqlalchemy as s
s.create_engine(os.environ["TEST_DATABASE_URL"]).connect().close()' >/dev/null 2>&1 || {
    echo "база lab_test недоступна — тесты с базой молча пропустились бы, прогон остановлен"
    exit 12
}
SH
    echo "/venv/bin/pytest -p no:cacheprovider$ARGS"
    cat <<'SH'
code=$?
echo "--- ruff"
/venv/bin/ruff check --no-cache . || { [ "$code" = 0 ] && code=11; }
exit $code
SH
} | ssh "$HOST" "cat > $SRC/.lab-test-run.sh" || exit 1

ssh -n "$HOST" "bash $REPO/scripts/run-detached.sh $JOB $(q "docker compose \
-f deploy/docker-compose.yml run --rm -T -v $SRC:/src -v $VENV:/venv -w /src \
-e UV_NO_SYNC=0 -e UV_PROJECT_ENVIRONMENT=/venv --entrypoint sh worker \
/src/.lab-test-run.sh </dev/null")" || exit 1

n=0
until ssh -n "$HOST" "grep -q 'ГОТОВО код=' $LOG" 2>/dev/null; do
    n=$((n + 1))
    if [ "$n" -ge "$WAIT_MAX" ]; then
        echo "не дождался за $((WAIT_MAX * WAIT_S / 60)) мин — задача идёт дальше, лог $HOST:$LOG" >&2
        exit 1
    fi
    sleep "$WAIT_S"
done

OUT="$(ssh -n "$HOST" "grep -v '^ *Container \|^ *Network ' $LOG" \
    | sed -E 's#(://[^:/@]+):[^@/]*@#\1:***@#g')"
echo "$OUT" | grep -v '^ГОТОВО код='
CODE="$(echo "$OUT" | sed -n 's/^ГОТОВО код=//p' | tail -n 1)"

ssh -n "$HOST" "rm -rf $SRC /root/$JOB.sh; shred -u $LOG" || echo "убрать за собой не удалось" >&2
exit "${CODE:-1}"
