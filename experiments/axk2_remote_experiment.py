"""Bounded Azure Run Command experiment and chunked result retrieval."""
import argparse
import base64
import json
import subprocess
from pathlib import Path
from cloud_context import legacy_cloud_context

AZ, SUBSCRIPTION = legacy_cloud_context()

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("name")
parser.add_argument("--collect", action="store_true")
args = parser.parse_args()


def invoke(script, suffix):
    process = subprocess.run(
        [AZ, "vm", "run-command", "invoke",
         "--resource-group", "rg-axk2-a100-smoke-208d24c1", "--name", "axk2-a100-smoke",
         "--command-id", "RunShellScript", "--scripts", script,
         "--subscription", SUBSCRIPTION, "--output", "json"],
        text=True, capture_output=True, timeout=2100,
    )
    (ROOT / f"{args.name}-{suffix}.log").write_text(process.stdout + process.stderr)
    process.check_returncode()
    response = json.loads(process.stdout)
    return "\n".join(value.get("message", "") for value in response["value"])


if not args.collect:
    script = ""
    for name in ("axk2_cuda_smoke.py", f"{args.name}.py"):
        encoded = base64.b64encode((ROOT / name).read_text().encode()).decode()
        script += f"printf '%s' '{encoded}' | base64 -d > /tmp/{name}\n"
    script += (
        f"timeout 1200 /opt/axk2-validation/bin/python /tmp/{args.name}.py > /tmp/{args.name}.log 2>&1; "
        f"status=$?; tail -c 3000 /tmp/{args.name}.log; echo GUEST_EXIT=$status; exit $status"
    )
    print(invoke(script, "run"))
else:
    # Compress and split below Azure Run Command's 4-KB output limit.
    guest_path = "/tmp/" + args.name.replace("_", "-").replace("-trials", "-results") + ".json"
    offset = 0
    chunks = []
    while True:
        script = (
            "/opt/axk2-validation/bin/python -c 'import pathlib,zlib,base64; "
            f"s=base64.b64encode(zlib.compress(pathlib.Path(\"{guest_path}\").read_bytes(),9)).decode(); "
            f"print(\"DATA=\"+s[{offset}:{offset+2800}]); print(\"SIZE=\"+str(len(s)))'"
        )
        message = invoke(script, f"collect-{offset}")
        chunk = next(line[5:] for line in message.splitlines() if line.startswith("DATA="))
        size = int(next(line[5:] for line in message.splitlines() if line.startswith("SIZE=")))
        chunks.append(chunk)
        offset += len(chunk)
        if offset >= size:
            break
        assert chunk, "Missing data chunk"
    import zlib
    data = zlib.decompress(base64.b64decode("".join(chunks)))
    (ROOT / (args.name + "-results.json")).write_bytes(data)
    print(data.decode())
