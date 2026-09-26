#!/usr/bin/env bash
# Ежемесячный снимок рейтинга публичных трейдеров и замер прошлых снимков ВПЕРЁД.
#
# Зачем регулярно: разделение истории пополам — лучшее, что можно сделать задним числом,
# но список кандидатов там отобран по прибыли за всю историю, включая измеряемую половину.
# Снимок этого изъяна лишён: будущего не видел никакой отбор. Один прогон в месяц —
# и через три-шесть месяцев появляется ответ на живых данных.
#
# Ставится в крон так (общий сервер, рядом торгует живой бот соседа — держим раз в месяц
# и ночью; 04:10 занято ежедневным forward_journal лаборатории, поэтому 05:10):
#   10 5 1 * * bash /opt/crypto-trading-lab/scripts/copytrade_monthly.sh >> /root/copytrade.log 2>&1 # lab_copytrade_monthly
# (через `bash`: у файла в git нет флага запуска. Стоит в кроне Hostkey с 26.09.2026.)
#
# Ответ копится в томе данных: copytrade/forward.jsonl (строка на снимок и прогон).
#
# Три флага, без которых замер тихо бесполезен (найдено 26.09.2026, до первого запуска):
#   --include-losers — без проигравших список снова состоит из одних победителей;
#   --refresh у portfolios — иначе рейтинг каждый месяц считается по кривым первого дня;
#   --top 0 — в снимок идёт весь рейтинг: при топ-200 верхнюю четверть сравнивали
#             со следующими ста пятьюдесятью, то есть элиту с элитой.
set -euo pipefail

REPO=/opt/crypto-trading-lab
LOCK=/tmp/copytrade-monthly.lock

# Замок: два одновременных прогона удвоят запросы и упрутся в порог частоты биржи.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date -Is) прошлый прогон ещё идёт, пропускаем"
    exit 0
fi

cd "$REPO"
RUN="docker compose -f deploy/docker-compose.yml run --rm --no-deps -T \
  -v $REPO:/src -w /src --entrypoint sh worker -c"
PY=/app/.venv/bin/python

echo "$(date -Is) обновляем лидерборд (с проигравшими) и ВСЕ кривые капитала"
$RUN "$PY scripts/copytrade_screen.py --stage leaderboard --refresh --include-losers --root /app/data" </dev/null
$RUN "$PY scripts/copytrade_screen.py --stage portfolios --refresh --limit 100000 --pause 0.5 --root /app/data" </dev/null

echo "$(date -Is) снимок сегодняшнего рейтинга целиком, с плечом на день снимка"
$RUN "$PY scripts/copytrade_screen.py --stage snapshot --top 0 --pause 0.1 --root /app/data" </dev/null

echo "$(date -Is) что дали прошлые снимки"
$RUN "$PY scripts/copytrade_screen.py --stage forward --pause 0.5 --root /app/data" </dev/null
echo "$(date -Is) готово"
