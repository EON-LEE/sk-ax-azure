#!/usr/bin/env bash
# Operates the deployed A.X K2 demo through its admin API.
#
#   demo/ctl.sh status                 power, active job, link, queue, eval (JSON)
#   demo/ctl.sh on | off               start / stop the 2-node A100 GPU job (billing starts / stops)
#   demo/ctl.sh restart                replace the running GPU job with a fresh one
#   demo/ctl.sh eval start [suites] [repeats] [limit] [run]    e.g. eval start aime,kobalt 4 0
#   demo/ctl.sh eval stop
#   demo/ctl.sh password demo|admin    rotate a password and print the new one
#
# AXK2_URL (default https://axk2-a100-demo.azurewebsites.net) and AXK2_ADMIN_PASSWORD; without the latter it
# reads ~/.axk2-demo/passwords (written by deploy.sh) or asks. Needs curl and python3.
set -euo pipefail
KEEP=$HOME/.axk2-demo/passwords
URL=${AXK2_URL:-}
[ -n "$URL" ] || URL=$(sed -n 's/^url=//p' "$KEEP" 2>/dev/null || true)
URL=${URL:-https://axk2-a100-demo.azurewebsites.net}
PASS=${AXK2_ADMIN_PASSWORD:-}
[ -n "$PASS" ] || PASS=$(sed -n 's/^admin=//p' "$KEEP" 2>/dev/null || true)
if [ -z "$PASS" ]; then read -rsp "admin password: " PASS; echo; fi

call() {  # call PATH [JSON]
  local out code
  out=$(mktemp)
  if [ $# -gt 1 ]; then
    code=$(curl -sS -o "$out" -w '%{http_code}' -H "Authorization: Bearer $PASS" \
      -H 'Content-Type: application/json' -d "$2" "$URL$1")
  else
    code=$(curl -sS -o "$out" -w '%{http_code}' -H "Authorization: Bearer $PASS" "$URL$1")
  fi
  python3 -m json.tool --no-ensure-ascii "$out" 2>/dev/null || cat "$out"
  rm -f "$out"
  [ "${code:0:1}" = 2 ] || { echo "HTTP $code" >&2; return 1; }
}
json() { python3 -c 'import json, sys; print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))' "$@"; }

case "${1:-status}" in
  status) call /api/admin/state ;;
  on | off) call /api/admin/power "$(json state "$1")" ;;
  restart) call /api/admin/restart '{}' ;;
  eval)
    case "${2:-}" in
      start)
        python3 - "${3:-}" "${4:-}" "${5:-0}" "${6:-}" > /tmp/axk2-eval.json <<'PY'
import json, sys
suites, repeats, limit, run = sys.argv[1:5]
body = {"action": "start", "suites": suites, "repeats": repeats, "limit": int(limit)}
if run:
    body["run"] = run
print(json.dumps(body))
PY
        call /api/admin/eval "$(cat /tmp/axk2-eval.json)"; rm -f /tmp/axk2-eval.json ;;
      stop) call /api/admin/eval '{"action":"stop"}' ;;
      *) echo "usage: $0 eval start [suites] [repeats] [limit] [run] | eval stop" >&2; exit 2 ;;
    esac ;;
  password)
    case "${2:-}" in
      demo | admin)
        new=$(call /api/admin/password "$(json role "$2")" |
          python3 -c 'import json, sys; print(json.load(sys.stdin)["password"])')
        echo "new $2 password: $new"
        if [ -f "$KEEP" ]; then
          ( umask 077; { grep -v "^$2=" "$KEEP" || true; echo "$2=$new"; } > "$KEEP.tmp" && mv "$KEEP.tmp" "$KEEP" )
          echo "updated $KEEP" >&2
        fi ;;
      *) echo "usage: $0 password demo|admin" >&2; exit 2 ;;
    esac ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
