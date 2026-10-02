"""Approved two-node trial only. Fixed expiration prevents accidental later reuse."""
import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from cloud_context import legacy_cloud_context

ROOT = Path(__file__).resolve().parent
GROUP = "rg-axk2-a100-dist-208d24c1"
AZ, SUB = legacy_cloud_context()
KEYDIR = Path("/tmp/axk2-dist-208d24c1")
parser = argparse.ArgumentParser()
parser.add_argument("stage", choices=["init", "0", "1"])
args = parser.parse_args()


def az(label, *argv, timeout=900):
    p = subprocess.run(
        [AZ, *argv, "--subscription", SUB, "--output", "json"],
        text=True, capture_output=True, timeout=timeout,
    )
    (ROOT / f"distributed-{args.stage}-{label}.log").write_text(p.stdout + p.stderr)
    if p.returncode:
        print(p.stderr, flush=True)
        p.check_returncode()
    return json.loads(p.stdout) if p.stdout.strip() else None


assert datetime.now(timezone.utc) < datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc), "Provisioning window expired."
if args.stage == "init":
    assert not az("exists", "group", "exists", "--name", GROUP)
    KEYDIR.mkdir(mode=0o700, exist_ok=False)
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(KEYDIR / "key"),
                    "-C", "temporary-axk2-distributed"], check=True, capture_output=True)
    az("group", "group", "create", "--name", GROUP, "--location", "eastus2",
       "--tags", "purpose=axk2-a100-distributed", "session=208d24c1", "expiresUtc=2026-10-02T05:25:00Z")
    az("vnet", "network", "vnet", "create", "--resource-group", GROUP,
       "--name", "axk2-dist-vnet", "--address-prefixes", "10.42.0.0/16",
       "--subnet-name", "axk2-dist-subnet", "--subnet-prefixes", "10.42.0.0/24")
    az("nsg", "network", "nsg", "create", "--resource-group", GROUP, "--name", "axk2-dist-nsg")
    print("Dedicated VNet and NSG created. No Internet inbound allow rule.", flush=True)
else:
    rank = int(args.stage)
    name = f"axk2-dist-{rank}"
    group = az("ownership", "group", "show", "--name", GROUP)
    assert group["tags"]["session"] == "208d24c1"
    try:
        result = az(
            "vm", "vm", "create", "--resource-group", GROUP, "--name", name,
            "--location", "eastus2", "--size", "Standard_NC24ads_A100_v4",
            "--image", "microsoft-dsvm:ubuntu-2204:2204-gen2:25.06.18",
            "--admin-username", "axk2test", "--authentication-type", "ssh",
            "--ssh-key-values", str(KEYDIR / "key.pub"), "--priority", "Spot",
            "--eviction-policy", "Delete", "--max-price", "3.673",
            "--vnet-name", "axk2-dist-vnet", "--subnet", "axk2-dist-subnet",
            "--nsg", "axk2-dist-nsg", "--nsg-rule", "NONE",
            "--private-ip-address", f"10.42.0.{4+rank}", "--public-ip-sku", "Standard",
            "--storage-sku", "StandardSSD_LRS", "--os-disk-size-gb", "150",
            "--os-disk-delete-option", "Delete", "--nic-delete-option", "Delete",
            "--tags", "purpose=axk2-a100-distributed", "session=208d24c1", f"rank={rank}",
        )
        az("shutdown", "vm", "auto-shutdown", "--resource-group", GROUP, "--name", name, "--time", "0520")
        print(json.dumps(result), flush=True)
    except Exception:
        # Failure must stop charges on this rank; coordinator handles group cleanup.
        az("deallocate-on-failure", "vm", "deallocate", "--resource-group", GROUP, "--name", name)
        raise
