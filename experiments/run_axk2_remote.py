"""Transfer local smoke-test files through Azure Run Command, without public SSH."""

import argparse
import base64
import json
import subprocess
import zlib
from pathlib import Path
from cloud_context import legacy_cloud_context

ROOT = Path(__file__).resolve().parent
AZ, SUBSCRIPTION = legacy_cloud_context()
parser = argparse.ArgumentParser()
parser.add_argument("stage", choices=["setup", "test", "collect"])
args = parser.parse_args()
if args.stage == "setup":
    encoded = base64.b64encode((ROOT / "setup_axk2_cuda.sh").read_text().encode()).decode()
    script = (
        f"printf '%s' '{encoded}' | base64 -d > /tmp/setup_axk2_cuda.sh\n"
        "bash /tmp/setup_axk2_cuda.sh; status=$?; tail -c 3200 /tmp/axk2-setup.log; exit $status"
    )
elif args.stage == "test":
    encoded = base64.b64encode((ROOT / "axk2_cuda_smoke.py").read_bytes()).decode()
    script = (
        f"printf '%s' '{encoded}' | base64 -d > /tmp/axk2_cuda_smoke.py\n"
        "timeout 1800 /opt/axk2-validation/bin/python /tmp/axk2_cuda_smoke.py "
        "> /tmp/axk2-cuda-test.log 2>&1; status=$?; "
        "tail -c 3200 /tmp/axk2-cuda-test.log; exit $status"
    )
else:
    script = (
        "/opt/axk2-validation/bin/python -c '"
        "import base64,zlib,pathlib; "
        "s=base64.b64encode(zlib.compress(pathlib.Path(\"/tmp/axk2-cuda-results.json\").read_bytes(),9)).decode(); "
        "assert len(s)<3500, len(s); print(\"AXK2_RESULT=\"+s)'"
    )
process = subprocess.run(
    [AZ, "vm", "run-command", "invoke", "--resource-group", "rg-axk2-a100-smoke-208d24c1",
     "--name", "axk2-a100-smoke", "--command-id", "RunShellScript", "--scripts", script,
     "--subscription", SUBSCRIPTION, "--output", "json"],
    text=True, capture_output=True, timeout=2400,
)
(ROOT / f"a100-remote-{args.stage}.json").write_text(process.stdout)
(ROOT / f"a100-remote-{args.stage}.stderr.log").write_text(process.stderr)
print(process.stdout, flush=True)
if process.returncode:
    print(process.stderr, flush=True)
    raise subprocess.CalledProcessError(process.returncode, "Azure Run Command")
# Run Command's envelope can succeed even when the guest script fails.
response = json.loads(process.stdout)
if args.stage == "setup":
    assert any("AXK2_SETUP_PASSED" in v.get("message", "") for v in response["value"]), "Guest setup did not pass."
elif args.stage == "collect":
    messages = "\n".join(v.get("message", "") for v in response["value"])
    encoded = next(line.split("=", 1)[1] for line in messages.splitlines() if line.startswith("AXK2_RESULT="))
    result = zlib.decompress(base64.b64decode(encoded))
    (ROOT / "axk2-a100-results.json").write_bytes(result)
    print("Saved full GPU result: axk2-a100-results.json")
