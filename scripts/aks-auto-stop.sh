#!/usr/bin/env bash
# Stop both AKS clusters at a wall-clock deadline (safety net against idle billing).
#
#   scripts/aks-auto-stop.sh 6         # stop 6 hours from now
#   echo 2026-10-26T02:00:00Z > logs/aks-auto-stop.deadline   # move the deadline
#   touch logs/aks-auto-stop.cancel    # cancel
#
# The deadline is compared with the wall clock every minute, so a laptop
# suspend delays the stop only until the machine wakes up.
set -euo pipefail
cd "$(dirname "$0")/.."
env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'"; }
AZURE_RESOURCE_GROUP=$(env_value AZURE_RESOURCE_GROUP)
AZURE_PRIMARY_CLUSTER=$(env_value AZURE_PRIMARY_CLUSTER)
AZURE_SECONDARY_CLUSTER=$(env_value AZURE_SECONDARY_CLUSTER)

mkdir -p logs
deadline_file=logs/aks-auto-stop.deadline
cancel_file=logs/aks-auto-stop.cancel
rm -f "$cancel_file"
date -u -d "+${1:-6} hours" +%FT%TZ > "$deadline_file"
echo "[auto-stop] clusters will be stopped at $(cat "$deadline_file")"

while true; do
  if [[ -f "$cancel_file" ]]; then echo "[auto-stop] cancelled at $(date -u +%FT%TZ)"; exit 0; fi
  if (( $(date -u +%s) >= $(date -u -d "$(cat "$deadline_file")" +%s) )); then break; fi
  sleep 60
done

echo "[auto-stop] deadline reached at $(date -u +%FT%TZ); stopping clusters"
az aks stop -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_PRIMARY_CLUSTER" &
az aks stop -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_SECONDARY_CLUSTER" &
wait
az aks list -g "$AZURE_RESOURCE_GROUP" --query "[].{name:name,power:powerState.code}" -o tsv
