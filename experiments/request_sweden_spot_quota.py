"""Bounded REST retry of the same user-approved quota value, never VM creation."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from cloud_context import legacy_cloud_context

ROOT = Path(__file__).resolve().parent
AZ, SUBSCRIPTION = legacy_cloud_context()
SCOPE = f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Compute/locations/swedencentral"
command = [
    AZ, "rest", "--method", "put",
    "--url", f"https://management.azure.com{SCOPE}/providers/Microsoft.Quota/quotas/lowPriorityCores?api-version=2023-02-01",
    "--body", "@" + str(ROOT / "sweden-spot-quota-body.json"), "--output", "json",
    "--subscription", SUBSCRIPTION,
]
record = {
    "scope": SCOPE, "requested_limit": 192,
    "started_at_utc": datetime.now(timezone.utc).isoformat(),
    "reason": "Original az quota create client waited without response; stopped local client only. Reusing identical approved quota target/value through bounded REST.",
}
try:
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    record.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
except subprocess.TimeoutExpired as error:
    record.update(status="CLIENT_TIMEOUT_REMOTE_OUTCOME_UNKNOWN", error=str(error))
record["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
(ROOT / "sweden-spot-quota-rest-result.json").write_text(json.dumps(record, indent=2))
print(json.dumps(record, indent=2))
if record.get("exit_code") != 0:
    raise SystemExit(1)
