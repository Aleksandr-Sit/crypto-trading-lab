#!/usr/bin/env bash
# Targeted deployment; both running services must acknowledge drain protocol 1.
# Older versions are refused: journal open=0 cannot prevent a concurrent new entry.
# Invoke through run-detached.sh. --dry-run never pauses or restarts anything.

main() {
  set -euo pipefail
  local wait_min=15 dry=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --wait-min) wait_min="${2:?missing minutes}"; shift 2 ;;
      --dry-run) dry=1; shift ;;
      *) echo "Unknown argument: $1" >&2; return 1 ;;
    esac
  done
  [[ "$wait_min" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid wait minutes" >&2; return 1; }
  cd "${CRASH_DEPLOY_REPO:-/opt/crypto-trading-lab}"
  exec 9>"${CRASH_DEPLOY_LOCK:-/tmp/cryptosmx-deploy.lock}"
  flock -n 9 || { echo "Another CryptosMX deployment is running" >&2; return 1; }
  local dc=(docker compose -f deploy/docker-compose.yml)
  local py=/app/.venv/bin/python ctl=scripts/crash_bounce_control.py
  local service cid protocol free_kb
  local -A old=()
  for service in crash-paper crash-forward; do
    cid="$("${dc[@]}" ps -q "$service")"
    [[ -n "$cid" ]] || { echo "$service is absent" >&2; return 1; }
    old[$service]="$cid"
    protocol="$(docker exec "$cid" "$py" "$ctl" protocol \
      --service "${service#crash-}" 2>/dev/null || true)"
    [[ "$protocol" == 1 ]] || {
      echo "REFUSED: running $service lacks drain protocol 1; no safe legacy fallback." >&2
      return 5
    }
  done
  free_kb="$(df --output=avail -k / | tail -1 | tr -d ' ')"
  (( free_kb >= 1024 * 1024 )) || { echo "Less than 1 GB free" >&2; return 4; }
  if (( dry )); then
    echo "Preflight passed; only crash-paper and crash-forward would be updated."
    return 0
  fi
  [[ -z "$(git status --porcelain)" ]] || { echo "Dirty server checkout" >&2; return 1; }
  local target
  git fetch --quiet
  target="$(git rev-parse '@{upstream}')"
  # config/ is mounted live into other services: do not change it through this deploy.
  git diff --quiet HEAD "$target" -- config || {
    echo "REFUSED: upstream changes shared config/; use a separate reviewed deployment" >&2
    return 1
  }
  git merge --ff-only "$target"
  LAB_CODE_VERSION="$(git rev-parse HEAD)"
  export LAB_CODE_VERSION
  "${dc[@]}" build crash-paper crash-forward </dev/null

  # Globals survive main() returning so EXIT cleanup can release both leases.
  CRASH_DEPLOY_TOKEN="$(cat /proc/sys/kernel/random/uuid)"
  CRASH_DEPLOY_PAPER="${old[crash-paper]}"
  CRASH_DEPLOY_FORWARD="${old[crash-forward]}"
  CRASH_DEPLOY_RELEASE=1
  CRASH_DEPLOY_SNAPSHOT="$(mktemp /tmp/cryptosmx-drain.XXXXXX.json)"
  cleanup_crash_deploy() {
    local saved=$?
    if [[ "$CRASH_DEPLOY_RELEASE" == 1 ]]; then
      docker exec "$CRASH_DEPLOY_PAPER" /app/.venv/bin/python \
        scripts/crash_bounce_control.py cancel --service paper --token "$CRASH_DEPLOY_TOKEN" \
        >/dev/null 2>&1 || true
      docker exec "$CRASH_DEPLOY_FORWARD" /app/.venv/bin/python \
        scripts/crash_bounce_control.py cancel --service forward --token "$CRASH_DEPLOY_TOKEN" \
        >/dev/null 2>&1 || true
    fi
    rm -f -- "$CRASH_DEPLOY_SNAPSHOT"
    return "$saved"
  }
  trap cleanup_crash_deploy EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  local deadline=$((SECONDS + wait_min * 60)) ready=0
  while (( SECONDS < deadline )); do
    ready=1
    for service in crash-paper crash-forward; do
      cid="${old[$service]}"
      [[ "$("${dc[@]}" ps -q "$service")" == "$cid" ]] || {
        echo "Container changed during deployment: $service" >&2; return 1;
      }
      docker exec "$cid" "$py" "$ctl" request --service "${service#crash-}" \
        --token "$CRASH_DEPLOY_TOKEN"
      if ! docker exec "$cid" "$py" "$ctl" status --service "${service#crash-}" \
        --token "$CRASH_DEPLOY_TOKEN" >/dev/null; then ready=0; fi
    done
    (( ready )) && break
    sleep 2
  done
  (( ready )) || { echo "Drain timed out; no containers restarted" >&2; return 2; }
  # Latch the fence before stop: it cannot expire and allow a new entry during a slow stop.
  for service in crash-paper crash-forward; do
    docker exec "${old[$service]}" "$py" "$ctl" commit --service "${service#crash-}" \
      --token "$CRASH_DEPLOY_TOKEN"
  done
  ready=0
  for ((deadline = SECONDS + 30; SECONDS < deadline; )); do
    ready=1
    for service in crash-paper crash-forward; do
      docker exec "${old[$service]}" "$py" "$ctl" status --service "${service#crash-}" \
        --token "$CRASH_DEPLOY_TOKEN" --committed >/dev/null || ready=0
    done
    (( ready )) && break
    sleep 1
  done
  (( ready )) || { echo "Committed pause not acknowledged; no restart" >&2; return 2; }
  docker exec "${old[crash-forward]}" "$py" "$ctl" status --service forward \
    --token "$CRASH_DEPLOY_TOKEN" --committed >/dev/null
  docker exec "${old[crash-paper]}" "$py" "$ctl" status --service paper \
    --token "$CRASH_DEPLOY_TOKEN" --committed >"$CRASH_DEPLOY_SNAPSHOT"
  "${dc[@]}" stop --timeout 20 crash-paper crash-forward </dev/null
  "${dc[@]}" up -d --no-deps --no-build --pull never --force-recreate \
    crash-paper crash-forward </dev/null
  CRASH_DEPLOY_RELEASE=0

  local healthy=0 state version
  for ((deadline = SECONDS + 180; SECONDS < deadline; )); do
    healthy=1
    for service in crash-paper crash-forward; do
      cid="$("${dc[@]}" ps -q "$service")"
      [[ -n "$cid" ]] || { healthy=0; continue; }
      [[ "$cid" != "${old[$service]}" ]] || healthy=0
      version="$(docker exec "$cid" printenv LAB_CODE_VERSION 2>/dev/null || true)"
      [[ "$version" == "$LAB_CODE_VERSION" ]] || healthy=0
      state="$(docker inspect --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")"
      if [[ "$service" == crash-paper ]]; then
        [[ "$state" == healthy ]] || healthy=0
      else
        [[ "$state" == running ]] || healthy=0
        docker exec "$cid" "$py" -c \
          'import json,time,os; s=json.load(open("/tmp/lab/crash-forward.drain.status.json")); os.kill(s["pid"],0); assert not s["paused"] and (s["busy"] or 0<=time.time()-s["checked_at"]<5)' \
          >/dev/null 2>&1 || healthy=0
      fi
    done
    (( healthy )) && break
    sleep 2
  done
  (( healthy )) || { echo "New services failed checks; investigate, no blind rollback" >&2; return 1; }
  "${dc[@]}" exec -T crash-paper "$py" scripts/crash_bounce_paper.py startup \
    --out /app/data/crash-bounce-paper <"$CRASH_DEPLOY_SNAPSHOT"
  echo "Deployed $LAB_CODE_VERSION; accounts match drained state, open_lost=0."
}

main "$@"; exit $?
