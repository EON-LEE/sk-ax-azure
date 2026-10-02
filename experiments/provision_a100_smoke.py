"""One approved, bounded Spot A100 trial; never starts or changes existing VMs."""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from cloud_context import legacy_cloud_context

AZ, SUB = legacy_cloud_context()
GROUP = "rg-axk2-a100-smoke-208d24c1"
VM = "axk2-a100-smoke"
ROOT = Path(__file__).resolve().parent
KEYDIR = Path("/tmp/axk2-smoke-208d24c1")


def az(name, *args, timeout=120):
    process = subprocess.run(
        [AZ, *args, "--subscription", SUB, "--output", "json"],
        text=True, capture_output=True, timeout=timeout,
    )
    (ROOT / f"a100-{name}.log").write_text(process.stdout + "\n" + process.stderr)
    if process.returncode:
        print(process.stderr, flush=True)
        raise subprocess.CalledProcessError(process.returncode, args)
    return json.loads(process.stdout) if process.stdout.strip() else None


if __name__ == "__main__":
    if datetime.now(timezone.utc) >= datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc):
        raise RuntimeError("Expired provisioning window. Do not reuse without new cost guards.")
    if az("group-exists-before", "group", "exists", "--name", GROUP):
        raise RuntimeError("Test resource group already exists; refusing to modify it.")
    KEYDIR.mkdir(mode=0o700, exist_ok=True)
    if not (KEYDIR / "id_ed25519.pub").exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(KEYDIR / "id_ed25519"),
             "-C", "temporary-axk2-smoke"], check=True, capture_output=True,
        )
    az("group-create", "group", "create", "--name", GROUP, "--location", "eastus2",
       "--tags", "purpose=axk2-a100-smoke", "session=208d24c1", "expiresUtc=2026-10-02T04:25:00Z")
    try:
        result = az(
            "vm-create", "vm", "create", "--resource-group", GROUP, "--name", VM,
            "--location", "eastus2", "--size", "Standard_NC24ads_A100_v4",
            "--image", "microsoft-dsvm:ubuntu-2204:2204-gen2:25.06.18",
            "--admin-username", "axk2test", "--authentication-type", "ssh",
            "--ssh-key-values", str(KEYDIR / "id_ed25519.pub"),
            "--priority", "Spot", "--eviction-policy", "Delete", "--max-price", "3.673",
            "--nsg-rule", "NONE", "--public-ip-sku", "Standard",
            "--storage-sku", "StandardSSD_LRS", "--os-disk-size-gb", "150",
            "--os-disk-delete-option", "Delete", "--nic-delete-option", "Delete",
            "--tags", "purpose=axk2-a100-smoke", "session=208d24c1",
            timeout=900,
        )
        print(json.dumps({"vm": result, "created_at": datetime.now(timezone.utc).isoformat()}), flush=True)
        az("auto-shutdown", "vm", "auto-shutdown", "--resource-group", GROUP,
           "--name", VM, "--time", "0420")
        print("Azure auto-shutdown configured for 04:20 UTC; session cleanup at 04:25 UTC.", flush=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Provisioning/guard failed; deleting this trial's dedicated resource group.", flush=True)
        az("cleanup-failed-provision", "group", "delete", "--name", GROUP, "--yes", timeout=900)
        raise
