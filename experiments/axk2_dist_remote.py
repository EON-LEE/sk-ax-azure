"""Run bounded commands on the two approved experiment nodes, via Azure."""
import argparse
import ast
import base64
import json
import subprocess
import zlib
from pathlib import Path
from cloud_context import legacy_cloud_context

AZ, SUBSCRIPTION = legacy_cloud_context()

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("rank", type=int, choices=[0, 1])
parser.add_argument("stage", choices=["setup", "download", "prompts", "run", "collect", "status"])
parser.add_argument("--result", default="result")
args = parser.parse_args()


def invoke(script, suffix):
    process = subprocess.run(
        [AZ, "vm", "run-command", "invoke",
         "--resource-group", "rg-axk2-a100-dist-208d24c1", "--name", f"axk2-dist-{args.rank}",
         "--command-id", "RunShellScript", "--scripts", script,
         "--subscription", SUBSCRIPTION, "--output", "json"],
        text=True, capture_output=True, timeout=2700,
    )
    (ROOT / f"dist-{args.rank}-{args.stage}-{suffix}.log").write_text(process.stdout + process.stderr)
    process.check_returncode()
    response = json.loads(process.stdout)
    return "\n".join(v.get("message", "") for v in response["value"])


def transfer(names):
    script = "mkdir -p /opt/axk2-dist\n"
    for name in names:
        text = (ROOT / name).read_text()
        if name.endswith(".py"):
            ast.parse(text, filename=name)
        encoded = base64.b64encode(text.encode()).decode()
        script += f"printf '%s' '{encoded}' | base64 -d > /opt/axk2-dist/{name}\n"
    return script


if args.stage == "setup":
    script = transfer(["setup_axk2_cuda.sh"])
    script += "bash /opt/axk2-dist/setup_axk2_cuda.sh; s=$?; tail -c 3000 /tmp/axk2-setup.log; echo GUEST_EXIT=$s; exit $s"
    response = invoke(script, "setup")
    print(response)
    assert "AXK2_SETUP_PASSED" in response, "GPU setup failed."
elif args.stage in ("download", "prompts", "run"):
    names = ["axk2_dist_weights.py"]
    if args.stage == "run":
        names.append("axk2_dist_pipeline.py")
    if args.stage == "prompts":
        names.append("axk2_dist_prompts.py")
    script = transfer(names)
    entry = {"download": "axk2_dist_weights.py", "prompts": "axk2_dist_prompts.py",
             "run": "axk2_dist_pipeline.py"}[args.stage]
    script += (
        f"export RANK={args.rank} WORLD_SIZE=2 MASTER_ADDR=10.42.0.4 MASTER_PORT=29500 "
        "NCCL_IB_DISABLE=1 NCCL_SOCKET_IFNAME=eth0 GLOO_SOCKET_IFNAME=eth0 NCCL_DEBUG=WARN\n"
        f"timeout 2400 /opt/axk2-validation/bin/python /opt/axk2-dist/{entry} "
        f"> /opt/axk2-dist/{args.stage}.log 2>&1; s=$?; "
        f"tail -c 3000 /opt/axk2-dist/{args.stage}.log; echo GUEST_EXIT=$s; exit $s"
    )
    response = invoke(script, args.stage)
    print(response)
    assert "GUEST_EXIT=0" in response, "Guest experiment failed; inspect preserved log."
elif args.stage == "status":
    print(invoke("nvidia-smi; tail -c 1800 /opt/axk2-dist/download.log; tail -c 1800 /opt/axk2-dist/run.log", "status"))
else:
    chunks, offset = [], 0
    while True:
        script = (
            "/opt/axk2-validation/bin/python -c 'import pathlib,zlib,base64; "
            f"s=base64.b64encode(zlib.compress(pathlib.Path(\"/opt/axk2-dist/{args.result}.json\").read_bytes(),9)).decode(); "
            f"print(\"DATA=\"+s[{offset}:{offset+2800}]); print(\"SIZE=\"+str(len(s)))'"
        )
        response = invoke(script, f"{args.result}-{offset}")
        chunk = next(line[5:] for line in response.splitlines() if line.startswith("DATA="))
        size = int(next(line[5:] for line in response.splitlines() if line.startswith("SIZE=")))
        chunks.append(chunk)
        offset += len(chunk)
        if offset >= size:
            break
        assert chunk
    data = zlib.decompress(base64.b64decode("".join(chunks)))
    (ROOT / f"dist-rank{args.rank}-{args.result}.json").write_bytes(data)
    print(data.decode()[:12000])
