#!/bin/bash
# Последний замер КАЖДОЙ стратегии реестра одной таблицей — С РАБОЧЕЙ МАШИНЫ (Git Bash).
#
# Зачем: `lab measure show` показывает снимки одной стратегии, а каталог стилей
# (docs/research/strategies/README.md) сверяется со всем реестром сразу. 26.09.2026 этот
# запрос собирался руками через три слоя кавычек (ssh → docker → sh); теперь одна строка.
#
# Как пользоваться (из корня репозитория):
#   bash scripts/last-measurements.sh              # все стратегии
#   bash scripts/last-measurements.sh listing      # только id, где есть подстрока «listing»
#
# Колонки: стратегия | ступень | статус | режим замера | дата | окно | итог порога | критерии.
# Критерий с «+» пройден, с «-» нет; значение обрезано до 8 знаков. Итог порога —
# passed | failed | insufficient (меньше 30 сделок — вердикта нет, а не провал).
#
# Только чтение: ничего не пишет ни в базу, ни на диск сервера. SQL уходит в psql через
# stdin — у единственного получателя stdin ловушки «exec -T съедает скрипт» нет.
# Пароль базы не попадает в командную строку: psql берёт имя и базу из окружения контейнера.

set -eu

pat="${1:-}"
case "$pat" in
  *[!a-z0-9-]*) echo "подстрока id — только a-z, 0-9 и дефис" >&2; exit 2 ;;
esac

ssh vps-trader 'cd /opt/crypto-trading-lab && docker compose -f deploy/docker-compose.yml exec -T db sh -c "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -At -F \" | \""' <<SQL
with last as (
  select distinct on (strategy_id, mode) *
  from measurements order by strategy_id, mode, id desc)
select s.id, s.rung, s.status, l.mode, l.created_at::date,
  l.window_from::date || '..' || l.window_to::date,
  coalesce((l.threshold_json::jsonb)->>'status', '-'),
  (select string_agg(c->>'name' || '=' || left(coalesce(c->>'value', ''), 8)
                     || case when (c->>'passed')::bool then '+' else '-' end, ' ')
     from jsonb_array_elements((l.threshold_json::jsonb)->'criteria') c)
from strategies s left join last l on l.strategy_id = s.id
where s.id like '%${pat}%'
order by s.id, l.mode;
SQL
