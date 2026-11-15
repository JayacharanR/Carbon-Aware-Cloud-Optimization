#!/usr/bin/env bash
# Stop both AKS clusters at a wall-clock deadline (safety net against idle billing).
#
#   scripts/aks-auto-stop.sh 6         # stop 6 hours from now
#   echo 2026-10-26T02:00:00Z > logs/aks-auto-stop.deadline   # move the deadline
#   touch logs/aks-auto-stop.cancel    # cancel
#
# The deadline is compared with the wall clock every minute, so a laptop
# suspend delays the stop only until the machine wakes up.
#
# This is a safety net, so it fails towards stopping: a deadline file that
# cannot be read as a date leaves the last valid deadline in force, and a
# failed `az aks stop` is retried and reported through the exit status.
set -euo pipefail
cd "$(dirname "$0")/.."

log() { echo "[auto-stop] $*"; }
env_value() { { grep -E "^$1=" .env || true; } | tail -1 | cut -d= -f2- | tr -d "\"'"; }

hours=${1:-6}
if ! [[ $hours =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "usage: $0 [hours-from-now]   (got '$hours')" >&2
  exit 2
fi
[[ -f .env ]] || { echo "[auto-stop] .env not found in $(pwd)" >&2; exit 2; }
for name in AZURE_RESOURCE_GROUP AZURE_PRIMARY_CLUSTER AZURE_SECONDARY_CLUSTER; do
  value=$(env_value "$name")
  [[ -n $value ]] || { echo "[auto-stop] $name is not set in .env; nothing would be stopped" >&2; exit 2; }
  declare "$name=$value"
done
command -v az >/dev/null || { echo "[auto-stop] the Azure CLI (az) is not on PATH" >&2; exit 2; }

mkdir -p logs
deadline_file=logs/aks-auto-stop.deadline
cancel_file=logs/aks-auto-stop.cancel
poll_seconds=${AKS_AUTO_STOP_POLL_SECONDS:-60}
rm -f "$cancel_file"
# date(1) does not take fractional hours ("+1.5 hours"), so add seconds.
deadline_epoch=$(( $(date -u +%s) + $(awk -v h="$hours" 'BEGIN { printf "%d", h * 3600 }') ))
date -u -d "@${deadline_epoch}" +%FT%TZ > "$deadline_file"
log "clusters will be stopped at $(cat "$deadline_file")"

while true; do
  if [[ -f "$cancel_file" ]]; then log "cancelled at $(date -u +%FT%TZ)"; exit 0; fi
  requested=$(head -n 1 "$deadline_file" 2>/dev/null || true)
  if parsed=$(date -u -d "$requested" +%s 2>/dev/null) && [[ -n $requested ]]; then
    if (( parsed != deadline_epoch )); then
      deadline_epoch=$parsed
      log "deadline moved to $(date -u -d "@${deadline_epoch}" +%FT%TZ)"
    fi
  else
    log "cannot read '$requested' in $deadline_file as a date; keeping $(date -u -d "@${deadline_epoch}" +%FT%TZ)"
  fi
  if (( $(date -u +%s) >= deadline_epoch )); then break; fi
  sleep "$poll_seconds"
done

log "deadline reached at $(date -u +%FT%TZ); stopping clusters"
stop_cluster() {
  local attempt
  for attempt in 1 2 3; do
    if az aks stop -g "$AZURE_RESOURCE_GROUP" -n "$1"; then return 0; fi
    log "stop of $1 failed (attempt $attempt of 3)"
    sleep "$(( attempt * poll_seconds / 2 ))"
  done
  return 1
}
stop_cluster "$AZURE_PRIMARY_CLUSTER" & primary_pid=$!
stop_cluster "$AZURE_SECONDARY_CLUSTER" & secondary_pid=$!
failed=0
wait "$primary_pid" || failed=1
wait "$secondary_pid" || failed=1
az aks list -g "$AZURE_RESOURCE_GROUP" --query "[].{name:name,power:powerState.code}" -o tsv || true
if (( failed )); then
  log "AT LEAST ONE CLUSTER WAS NOT STOPPED and is still billing; stop it by hand"
  exit 1
fi
log "both clusters stopped"
