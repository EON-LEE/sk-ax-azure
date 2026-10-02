#!/usr/bin/env bash
# Create a policy-compliant Azure ML workspace and a low-priority 2 x ND96amsr_A100_v4 cluster in one
# region. Compute clusters cost nothing while they have zero nodes.
#
#   AZ=az SUB=<subscription-id> RG=<resource-group> bash aml/setup_region.sh <region> <workspace-name>
#
# Storage policy in this subscription forces publicNetworkAccess=Disabled and allowSharedKeyAccess=false,
# so the workspace uses a managed VNet with private endpoints, identity-based system datastores, and a
# system-assigned identity per compute cluster with Storage Blob Data Contributor on the workspace
# storage account only.
set -euo pipefail
REGION="${1:?region}"; WS="${2:?workspace name}"
AZ="${AZ:-az}"; : "${SUB:?SUB}"; : "${RG:?RG}"
TAGS="purpose=axk2-distributed-inference-poc owner=${OWNER_TAG:-unknown}"
C=(--subscription "$SUB" -g "$RG")

"$AZ" ml workspace create "${C[@]}" -n "$WS" -l "$REGION" --tags $TAGS \
  --managed-network allow_internet_outbound --system-datastores-auth-mode identity -o none
"$AZ" ml workspace provision-network "${C[@]}" -n "$WS" -o none
"$AZ" ml compute create "${C[@]}" -w "$WS" --name a100-nd96-lp --type AmlCompute \
  --size Standard_ND96amsr_A100_v4 --tier low_priority --min-instances 0 --max-instances 2 \
  --idle-time-before-scale-down 600 --identity-type SystemAssigned -o none
STORAGE=$("$AZ" ml workspace show "${C[@]}" -n "$WS" --query storage_account -o tsv)
PRINCIPAL=$("$AZ" ml compute show "${C[@]}" -w "$WS" -n a100-nd96-lp --query identity.principal_id -o tsv)
"$AZ" role assignment create --subscription "$SUB" --assignee-object-id "$PRINCIPAL" \
  --assignee-principal-type ServicePrincipal --role "Storage Blob Data Contributor" --scope "$STORAGE" -o none
USER_ID=$("$AZ" ad signed-in-user show --query id -o tsv)
"$AZ" role assignment create --subscription "$SUB" --assignee-object-id "$USER_ID" \
  --assignee-principal-type User --role "Storage Blob Data Contributor" --scope "$STORAGE" -o none
echo "ready: $WS ($REGION)"
