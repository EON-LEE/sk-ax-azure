#!/usr/bin/env bash
# Keep the same job queued in several regions and let the first region with real capacity win.
# As soon as one job gets its nodes, every other region's job is cancelled, so only one cluster is
# ever billed. A region that holds only part of its nodes for longer than PARTIAL_LIMIT seconds
# (paying for idle GPUs while Spot capacity for the rest never arrives) is dropped.
#
#   AZ=az SUB=<subscription-id> RG=<resource-group> bash aml/first_capacity_wins.sh ws1=job1 ws2=job2 ...
set -uo pipefail
AZ="${AZ:-az}"; : "${SUB:?SUB}"; : "${RG:?RG}"
INTERVAL="${INTERVAL:-60}"; PARTIAL_LIMIT="${PARTIAL_LIMIT:-900}"; COMPUTE="${COMPUTE:-a100-nd96-lp}"
declare -A JOBS PARTIAL_SINCE
for pair in "$@"; do JOBS["${pair%%=*}"]="${pair#*=}"; done

nodes() {
  "$AZ" rest --method get -o tsv --query "properties.properties.currentNodeCount" --url \
    "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.MachineLearningServices/workspaces/$1/computes/$COMPUTE?api-version=2024-04-01" 2>/dev/null
}
cancel() { "$AZ" ml job cancel --subscription "$SUB" -g "$RG" -w "$1" -n "$2" -o none 2>/dev/null; }

while [ "${#JOBS[@]}" -gt 0 ]; do
  for ws in "${!JOBS[@]}"; do
    job="${JOBS[$ws]}"
    status=$("$AZ" ml job show --subscription "$SUB" -g "$RG" -w "$ws" -n "$job" --query status -o tsv 2>/dev/null)
    count=$(nodes "$ws"); count="${count:-0}"
    echo "$(date -u +%FT%TZ) $ws $job status=$status nodes=$count"
    case "$status" in
      Preparing|Running|Finalizing|Completed)
        for other in "${!JOBS[@]}"; do
          [ "$other" = "$ws" ] || { cancel "$other" "${JOBS[$other]}"; echo "cancelled $other ${JOBS[$other]}"; }
        done
        echo "WINNER $ws $job"
        exit 0 ;;
      Failed|Canceled|CancelRequested)
        echo "dropping $ws ($status)"; unset "JOBS[$ws]"; continue ;;
    esac
    if [ "$count" -gt 0 ] 2>/dev/null; then
      PARTIAL_SINCE[$ws]="${PARTIAL_SINCE[$ws]:-$(date +%s)}"
      if [ $(( $(date +%s) - PARTIAL_SINCE[$ws] )) -gt "$PARTIAL_LIMIT" ]; then
        cancel "$ws" "$job"; echo "dropping $ws: partial allocation for over ${PARTIAL_LIMIT}s"; unset "JOBS[$ws]"
      fi
    else
      unset "PARTIAL_SINCE[$ws]"
    fi
  done
  sleep "$INTERVAL"
done
echo "NO WINNER: every region failed or was dropped"
exit 1
