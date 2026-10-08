#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
SUB=ME-M365CPI74210306-eonlee-1
NAME=axk2-hosted-model-api-trial
FILTER="https://graph.microsoft.com/v1.0/applications?%24filter=displayName%20eq%20%27${NAME}%27&%24select=id,appId,displayName,notes"
MARKER="Isolated Hosted AX model-only trial; no client credentials."
apps=$(az rest --method GET --subscription "$SUB" --resource https://graph.microsoft.com \
  --url "$FILTER" --query value -o json)
count=$(printf '%s' "$apps" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
if [ "$count" = 0 ]; then
  body='{
    "displayName":"axk2-hosted-model-api-trial",
    "signInAudience":"AzureADMyOrg",
    "notes":"Isolated Hosted AX model-only trial; no client credentials.",
    "api":{"requestedAccessTokenVersion":2},
    "appRoles":[{
      "allowedMemberTypes":["Application"],
      "description":"Invoke the bounded A.X Hosted model-only trial route.",
      "displayName":"Invoke Hosted A.X model",
      "id":"0b5dfe5a-fdaa-4e6d-b815-40620e07bf36",
      "isEnabled":true,
      "value":"Model.Invoke"
    }]
  }'
  app=$(az rest --method POST --subscription "$SUB" --resource https://graph.microsoft.com \
    --url https://graph.microsoft.com/v1.0/applications --body "$body" -o json)
elif [ "$count" = 1 ]; then
  app=$(printf '%s' "$apps" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[0]))')
  marker=$(printf '%s' "$app" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("notes",""))')
  [ "$marker" = "$MARKER" ] || { echo "Existing API identity is not owned by this trial." >&2; exit 1; }
else
  echo "Ambiguous trial API identity; no changes made." >&2
  exit 1
fi
object_id=$(printf '%s' "$app" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
app_id=$(printf '%s' "$app" | python3 -c 'import json,sys; print(json.load(sys.stdin)["appId"])')
az rest --method PATCH --subscription "$SUB" --resource https://graph.microsoft.com \
  --url "https://graph.microsoft.com/v1.0/applications/$object_id" \
  --body "{\"identifierUris\":[\"api://$app_id\"]}" -o none
sp_count=$(az rest --method GET --subscription "$SUB" --resource https://graph.microsoft.com \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals?%24filter=appId%20eq%20%27${app_id}%27&%24select=id" \
  --query 'length(value)' -o tsv)
if [ "$sp_count" = 0 ]; then
  az rest --method POST --subscription "$SUB" --resource https://graph.microsoft.com \
    --url https://graph.microsoft.com/v1.0/servicePrincipals --body "{\"appId\":\"$app_id\"}" -o none
elif [ "$sp_count" != 1 ]; then
  echo "Ambiguous API resource service principal." >&2
  exit 1
fi
echo "Trial API registration and resource service principal ready; no credential created."
echo "No Hosted caller app-role assignment has been made yet."
