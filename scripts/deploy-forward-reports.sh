#!/bin/bash
# Install only the reporting sidecar; existing trading/collector containers stay running.
set -euo pipefail
cd /opt/crypto-trading-lab
STATE=/root/forward-report-rollout-20261107
test ! -e "$STATE"
mkdir -m 700 "$STATE"
test -z "$(git status --porcelain)"
for service in worker bot web crash-paper crash-forward; do
    docker inspect --format '{{.Id}} {{.Image}} {{.State.StartedAt}}' \
        "crypto-trading-lab-$service-1" > "$STATE/$service.before"
done
docker exec -i crypto-trading-lab-worker-1 /app/.venv/bin/python3 - \
    < scripts/rotation_observation_status.py > "$STATE/observation.before.json"
git fetch origin main
git merge --ff-only origin/main
DC=(docker compose -f deploy/docker-compose.yml -f deploy/forward-report.compose.yml)
"${DC[@]}" config --quiet
"${DC[@]}" up -d --no-deps --no-build --force-recreate forward-report </dev/null
healthy=0
for _ in $(seq 1 24); do
    actual="$(docker inspect --format '{{.State.Health.Status}}' crypto-trading-lab-forward-report-1)"
    if test "$actual" = healthy; then healthy=1; break; fi
    sleep 5
done
test "$healthy" = 1
test "$(docker inspect --format '{{.Image}}' crypto-trading-lab-forward-report-1)" \
    = "$(docker inspect --format '{{.Image}}' crypto-trading-lab-worker-1)"
docker exec -i crypto-trading-lab-worker-1 /app/.venv/bin/python3 - \
    < scripts/rotation_observation_status.py > "$STATE/observation.after.json"
python3 - "$STATE/observation.before.json" "$STATE/observation.after.json" <<'PY'
import json
import sys
before, after = [json.load(open(path)) for path in sys.argv[1:]]
assert before['observations'] == after['observations'], 'Observation changed'
assert after['observations'][0]['revision'] == after['current_rules_revision']
assert after['collector']['observation_id'] == after['observations'][0]['id']
assert after['health_problems'] == {}, 'Collector unhealthy'
print('Observation unchanged; collector healthy')
PY
for service in worker bot web crash-paper crash-forward; do
    docker inspect --format '{{.Id}} {{.Image}} {{.State.StartedAt}}' \
        "crypto-trading-lab-$service-1" > "$STATE/$service.after"
    cmp "$STATE/$service.before" "$STATE/$service.after"
done
echo 'Reporting sidecar ready; due reports are queued by its loop. Verify delivery separately.'
