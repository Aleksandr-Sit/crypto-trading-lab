#!/bin/bash
# Exact approved release; run detached on vps-trader. Does not restart CryptosMX.
set -euo pipefail
cd /opt/crypto-trading-lab
DC=(docker compose -f deploy/docker-compose.yml)
STATE=/root/lab-forward-deploy-b6066dc
test ! -e "$STATE"
mkdir -m 700 "$STATE"
date -u +%FT%TZ > "$STATE/started-at"
git rev-parse HEAD > "$STATE/before-commit"
test -z "$(git status --porcelain)"
for service in worker bot web crash-paper crash-forward; do
    docker inspect --format '{{.Id}} {{.Image}} {{.State.StartedAt}}' \
        "crypto-trading-lab-$service-1" > "$STATE/$service.before"
done
tar -czf "$STATE/config-before.tar.gz" config
check_worker() {
    local extra
    extra="$(docker top crypto-trading-lab-worker-1 | tail -n +2 \
        | grep -v -E '(uv run python -m lab service worker|/app/.venv/bin/python3 -m lab service worker)$' || true)"
    test -z "$extra" || { echo 'Worker has an extra process; refusing restart'; return 1; }
}
check_worker
echo '[1/6] Fetch exact approved release'
git fetch origin main
target="$(git rev-parse b6066dc^{commit})"
git merge-base --is-ancestor HEAD "$target"
git merge --ff-only "$target"
test "$(git rev-parse HEAD)" = "$target"
export LAB_CODE_VERSION="$target"
echo '[2/6] Build only migrate/worker/bot/web'
"${DC[@]}" build migrate worker bot web </dev/null
check_worker
echo '[3/6] Stop database writers, back up database'
"${DC[@]}" stop worker bot web </dev/null
"${DC[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
    > "$STATE/db.dump"
test -s "$STATE/db.dump"
"${DC[@]}" exec -T db pg_restore --list < "$STATE/db.dump" > "$STATE/db.contents"
test -s "$STATE/db.contents"
echo '[4/6] Migrate and preview audited repair'
"${DC[@]}" run --rm --no-deps -T migrate </dev/null
"${DC[@]}" run --rm --no-deps -T worker uv run python \
    scripts/prepare_rotation_forward.py </dev/null
echo '[5/6] Repair rung atomically and start updated services'
"${DC[@]}" run --rm --no-deps -T worker uv run python \
    scripts/prepare_rotation_forward.py --apply </dev/null
"${DC[@]}" up -d --no-deps worker bot web </dev/null
echo '[6/6] Check health, versions and untouched CryptosMX containers'
healthy=0
for _ in $(seq 1 36); do
    healthy=1
    for service in worker bot web; do
        actual="$(docker inspect --format '{{.State.Health.Status}}' "crypto-trading-lab-$service-1")"
        test "$actual" = healthy || healthy=0
    done
    test "$healthy" = 1 && break
    sleep 5
done
test "$healthy" = 1
for service in worker bot web; do
    test "$(docker exec "crypto-trading-lab-$service-1" printenv LAB_CODE_VERSION)" = "$target"
done
for service in crash-paper crash-forward; do
    docker inspect --format '{{.Id}} {{.Image}} {{.State.StartedAt}}' \
        "crypto-trading-lab-$service-1" > "$STATE/$service.after"
    cmp "$STATE/$service.before" "$STATE/$service.after"
done
date -u +%FT%TZ > "$STATE/services-ready-at"
echo "Services ready: $target; backup: $STATE/db.dump"
echo 'Collector warmup and archive verification must still be checked separately.'
