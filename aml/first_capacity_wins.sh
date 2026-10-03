#!/usr/bin/env bash
# Keep the same job queued in several regions and let the first region with real capacity win.
# A region wins as soon as its cluster holds all NODES nodes (or its job is running). Every other
# region's job is then cancelled, and any loser cluster already holding nodes is deleted at once,
# so only one cluster keeps billing. A region that holds only part of its nodes for longer than
# PARTIAL_LIMIT seconds (paying for idle GPUs while Spot capacity for the rest never arrives) is
# dropped the same way.
#
#   AZ=az SUB=<subscription-id> RG=<resource-group> bash aml/first_capacity_wins.sh ws1=job1 ws2=job2 ...
set -uo pipefail
AZ="${AZ:-az}"; : "${SUB:?SUB}"; : "${RG:?RG}"
INTERVAL="${INTERVAL:-45}"; PARTIAL_LIMIT="${PARTIAL_LIMIT:-900}"; COMPUTE="${COMPUTE:-a100-nd96-lp}"
NODES="${NODES:-2}"
declare -A JOBS PARTIAL_SINCE
for pair in "$@"; do JOBS["${pair%%=*}"]="${pair#*=}"; done

nodes() {
  "$AZ" rest --method get -o tsv --query "properties.properties.currentNodeCount" --url \
    "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.MachineLearningServices/workspaces/$1/computes/$COMPUTE?api-version=2024-04-01" 2>/dev/null
}
cancel() { "$AZ" ml job cancel --subscription "$SUB" -g "$RG" -w "$1" -n "$2" -o none 2>/dev/null; }
release() {  # cancel the job; delete the cluster if it already holds nodes
  cancel "$1" "$2"
  local held; held=$(nodes "$1"); held="${held:-0}"
  if [ "$held" -gt 0 ] 2>/dev/null; then
    "$AZ" ml compute delete --subscription "$SUB" -g "$RG" -w "$1" -n "$COMPUTE" --yes -o none 2>/dev/null
    echo "released $1: cancelled $2 and deleted $COMPUTE holding $held node(s)"
  else
    echo "released $1: cancelled $2"
  fi
}

while [ "${#JOBS[@]}" -gt 0 ]; do
  for ws in "${!JOBS[@]}"; do
    job="${JOBS[$ws]}"
    status=$("$AZ" ml job show --subscription "$SUB" -g "$RG" -w "$ws" -n "$job" --query status -o tsv 2>/dev/null)
    count=$(nodes "$ws"); count="${count:-0}"
    echo "$(date -u +%FT%TZ) $ws $job status=$status nodes=$count"
    case "$status" in
      Failed|Canceled|CancelRequested)
        echo "dropping $ws ($status)"; unset "JOBS[$ws]"; continue ;;
    esac
    if [ "$count" -ge "$NODES" ] 2>/dev/null || [[ "$status" =~ ^(Preparing|Running|Finalizing|Completed)$ ]]; then
      for other in "${!JOBS[@]}"; do
        [ "$other" = "$ws" ] || release "$other" "${JOBS[$other]}"
      done
      echo "WINNER $ws $job"
      exit 0
    fi
    if [ "$count" -gt 0 ] 2>/dev/null; then
      PARTIAL_SINCE[$ws]="${PARTIAL_SINCE[$ws]:-$(date +%s)}"
      if [ $(( $(date +%s) - PARTIAL_SINCE[$ws] )) -gt "$PARTIAL_LIMIT" ]; then
        release "$ws" "$job"; echo "dropping $ws: partial allocation for over ${PARTIAL_LIMIT}s"; unset "JOBS[$ws]"
      fi
    else
      unset "PARTIAL_SINCE[$ws]"
    fi
  done
  sleep "$INTERVAL"
done
echo "NO WINNER: every region failed or was dropped"
exit 1