#!/usr/bin/env bash
# Deploys the A.X K2 demo frontend (demo/frontend) to Azure App Service. Safe to run again: it creates what is
# missing, keeps the existing passwords and session secret, and redeploys the code.
#
#   AXK2_SUB=<subscription id> bash demo/deploy.sh
#
# Optional: AXK2_RG, AXK2_LOCATION, AXK2_PLAN, AXK2_APP, AXK2_WORKSPACES (region=workspace,...; race order),
# AXK2_COMPUTE, AXK2_OPEN_DEMO (1 = the chat page needs no password; the admin page still does; default 1).
# The GPU clusters and workspaces must already exist (aml/setup_region.sh). On the first run it
# generates the demo and admin passwords, prints them once and keeps a copy in ~/.axk2-demo/passwords (0600).
# Needs az (logged in), python3 and curl; python builds the zip.
set -euo pipefail
export PATH=$HOME/.local/bin:$PATH
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SUB=${AXK2_SUB:?set AXK2_SUB to the subscription id}
RG=${AXK2_RG:-rg-axk2-demo-208d24c1}
LOC=${AXK2_LOCATION:-koreacentral}
PLAN=${AXK2_PLAN:-asp-axk2-demo-208d24c1}
APP=${AXK2_APP:-axk2-a100-demo}
SUFFIX=${RG#rg-axk2-demo-}
WORKSPACES=${AXK2_WORKSPACES:-uksouth=mlw-axk2-uks-r5-$SUFFIX,italynorth=mlw-axk2-itn-r5-$SUFFIX,francecentral=mlw-axk2-frc-r5-$SUFFIX}
COMPUTE=${AXK2_COMPUTE:-a100-nd96-lp}
OPEN_DEMO=${AXK2_OPEN_DEMO:-1}
KEEP=$HOME/.axk2-demo
az account set -s "$SUB"

echo "== App Service plan and web app ($LOC)"
az appservice plan show -g "$RG" -n "$PLAN" -o none 2>/dev/null ||
  az appservice plan create -g "$RG" -n "$PLAN" --is-linux --sku B1 -l "$LOC" -o none
az webapp show -g "$RG" -n "$APP" -o none 2>/dev/null ||
  az webapp create -g "$RG" -p "$PLAN" -n "$APP" --runtime "PYTHON:3.12" -o none
# One uvicorn process: the link registry, the chat queue and the supervisor live in memory.
az webapp config set -g "$RG" -n "$APP" --web-sockets-enabled true --always-on true --http20-enabled true \
  --min-tls-version 1.2 --ftps-state Disabled \
  --startup-file "python -m uvicorn app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers" -o none
az webapp update -g "$RG" -n "$APP" --https-only true -o none
HOST=$(az webapp show -g "$RG" -n "$APP" --query defaultHostName -o tsv)

echo "== managed identity: AzureML Data Scientist on $RG (submits and cancels the GPU jobs)"
MI=$(az webapp identity assign -g "$RG" -n "$APP" --query principalId -o tsv)
for i in 1 2 3 4 5 6; do
  az role assignment create --assignee-object-id "$MI" --assignee-principal-type ServicePrincipal \
    --role "AzureML Data Scientist" --scope "/subscriptions/$SUB/resourceGroups/$RG" -o none && break
  sleep 20  # a new identity takes a moment to replicate
done

echo "== app settings"
have() { az webapp config appsettings list -g "$RG" -n "$APP" --query "[?name=='$1'] | length(@)" -o tsv; }
hash() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from store import hash_password; print(hash_password(sys.argv[2]))' \
  "$ROOT/demo/frontend" "$1"; }
fresh() { python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from store import new_password; print(new_password())' \
  "$ROOT/demo/frontend"; }
SECRETS=()
[ "$(have AXK2_SESSION_SECRET)" = 1 ] || SECRETS+=("AXK2_SESSION_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')")
NEW=""
for role in demo admin; do
  name=AXK2_$(echo $role | tr a-z A-Z)_PASSWORD_HASH
  if [ "$(have "$name")" != 1 ]; then
    password=$(fresh)
    SECRETS+=("$name=$(hash "$password")")
    NEW="$NEW$role=$password"$'\n'
  fi
done
if [ ${#SECRETS[@]} -gt 0 ]; then
  az webapp config appsettings set -g "$RG" -n "$APP" -o none --settings "${SECRETS[@]}"
fi
if [ -n "$NEW" ]; then
  mkdir -p "$KEEP" && chmod 700 "$KEEP"
  ( umask 077; printf 'url=https://%s\n%s' "$HOST" "$NEW" > "$KEEP/passwords" )
  echo "New passwords (shown once; a copy is in $KEEP/passwords):"
  printf '%s' "$NEW" | sed 's/^/  /'
fi
az webapp config appsettings set -g "$RG" -n "$APP" -o none --settings \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true FORWARDED_ALLOW_IPS='*' WEBSITES_PORT=8000 AXK2_DATA=/home/data \
  AXK2_PUBLIC_URL="https://$HOST" AXK2_SUBSCRIPTION="$SUB" AXK2_RESOURCE_GROUP="$RG" \
  AXK2_WORKSPACES="$WORKSPACES" AXK2_COMPUTE="$COMPUTE" AXK2_OPEN_DEMO="$OPEN_DEMO"
OLD=$(az webapp config appsettings list -g "$RG" -n "$APP" --query "[?starts_with(name, 'SPIKE_')].name" -o tsv | tr -d '\r')
[ -z "$OLD" ] || az webapp config appsettings delete -g "$RG" -n "$APP" -o none --setting-names $OLD

echo "== bundle"
ZIP=$(mktemp -d)/axk2-demo.zip
python3 - "$ROOT" "$ZIP" <<'PY'
import sys, zipfile
from pathlib import Path
root, target = Path(sys.argv[1]), sys.argv[2]
front = root / "demo" / "frontend"
files = {p.relative_to(front).as_posix(): p for p in front.rglob("*")
         if p.is_file() and "__pycache__" not in p.parts}
files["aml/render_job.py"] = root / "aml" / "render_job.py"
files["aml/jobs/demo-fp8-nd96.yml"] = root / "aml" / "jobs" / "demo-fp8-nd96.yml"
for p in (root / "aml" / "src").iterdir():
    if p.is_file() and p.suffix in {".py", ".sh", ".json"}:
        files["aml/src/" + p.name] = p
report = root / "docs" / "report"
for p in [report / "skt_published.json", report / "figures" / "throughput.json", *sorted((report / "figures").glob("*.png"))]:
    files["static/results/" + p.name] = p
evidence = root / "evidence" / "a100-benchmarks.json"
if evidence.is_file():
    files["static/results/" + evidence.name] = evidence
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
    for name, path in sorted(files.items()):
        data = path.read_bytes()
        if path.suffix != ".png":
            data = data.replace(b"\r\n", b"\n")
        z.writestr(name, data)
print(f"{len(files)} files")
PY

echo "== deploy (Oryx installs requirements.txt on the server)"
# The gateway may answer 504, or the status poll may hang after the site is up; /healthz below is the real check.
az webapp deploy -g "$RG" -n "$APP" --src-path "$ZIP" --type zip --async false --timeout 600000 -o none || echo "deploy returned $?; waiting for /healthz"
rm -rf "$(dirname "$ZIP")"
for i in $(seq 1 45); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/healthz" || true)
  [ "$code" = 200 ] && { echo "https://$HOST is up"; exit 0; }
  sleep 20
done
echo "https://$HOST/healthz did not answer 200; see: az webapp log tail -g $RG -n $APP" >&2
exit 1
