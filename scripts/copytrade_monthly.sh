#!/usr/bin/env bash
# Ежемесячный снимок рейтинга публичных трейдеров и замер прошлых снимков ВПЕРЁД.
#
# Зачем регулярно: разделение истории пополам — лучшее, что можно сделать задним числом,
# но список кандидатов там отобран по прибыли за всю историю, включая измеряемую половину.
# Снимок этого изъяна лишён: будущего не видел никакой отбор. Один прогон в месяц —
# и через три-шесть месяцев появляется ответ на живых данных.
#
# Ставится в крон так (общий сервер, рядом торгует живой бот соседа — держим раз в месяц
# и ночью, когда никто не мерит):
#   10 4 1 * * /opt/crypto-trading-lab/scripts/copytrade_monthly.sh >> /root/copytrade.log 2>&1
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
RUN="docker compose -f deploy/docker-compose.yml run --rm --no-deps \
  -v $REPO:/src -w /src --entrypoint sh worker -c"
PY=/app/.venv/bin/python

echo "$(date -Is) обновляем лидерборд и кривые капитала"
$RUN "$PY scripts/copytrade_screen.py --stage leaderboard --refresh --root /app/data"
$RUN "$PY scripts/copytrade_screen.py --stage portfolios --limit 3000 --root /app/data"

echo "$(date -Is) снимок сегодняшнего рейтинга"
$RUN "$PY scripts/copytrade_screen.py --stage snapshot --top 200 --root /app/data"

echo "$(date -Is) что дали прошлые снимки"
$RUN "$PY scripts/copytrade_screen.py --stage forward --root /app/data"
