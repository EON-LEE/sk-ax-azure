"""Delete only the explicitly approved, already-deallocated experiment group."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from cloud_context import legacy_cloud_context

ROOT = Path(__file__).resolve().parent
GROUP = "rg-axk2-a100-dist-208d24c1"
AZ, SUBSCRIPTION = legacy_cloud_context()


def az(label, *arguments):
    result = subprocess.run(
        [AZ, *arguments, "--subscription", SUBSCRIPTION, "--output", "json"],
        text=True, capture_output=True, timeout=900,
    )
    (ROOT / f"dist-cleanup-{label}.log").write_text(result.stdout + result.stderr)
    result.check_returncode()
    return json.loads(result.stdout) if result.stdout.strip() else None


if az("exists-before", "group", "exists", "--name", GROUP):
    group = az("ownership", "group", "show", "--name", GROUP)
    assert group["tags"]["session"] == "208d24c1"
    assert group["tags"]["purpose"] == "axk2-a100-distributed"
    vms = az("vms", "vm", "list", "--resource-group", GROUP, "--show-details")
    assert {vm["name"] for vm in vms} <= {"axk2-dist-0", "axk2-dist-1"}
    for vm in vms:
        assert vm["tags"]["session"] == "208d24c1"
        assert vm["powerState"] == "VM deallocated", (vm["name"], vm["powerState"])
    az("delete", "group", "delete", "--name", GROUP, "--yes")
exists = az("exists-after", "group", "exists", "--name", GROUP)
assert exists is False, "Deletion has not been confirmed"
keydir = Path("/tmp/axk2-dist-208d24c1")
for key in (keydir / "key", keydir / "key.pub"):
    key.unlink(missing_ok=True)
if keydir.exists():
    keydir.rmdir()
confirmation = {
    "resource_group": GROUP,
    "group_exists": exists,
    "verified_at_utc": datetime.now(timezone.utc).isoformat(),
    "temporary_ssh_keys_removed": not keydir.exists(),
    "scope": "Two-node trial only. No other resource group changed.",
}
(ROOT / "dist-cleanup-confirmation.json").write_text(json.dumps(confirmation, indent=2))
print(json.dumps(confirmation, indent=2))
