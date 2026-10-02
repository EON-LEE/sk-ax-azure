"""Overlay the SKT-AI/vllm axk2-v0.23.0 Python sources onto the stock vLLM 0.23.0 image.

The fork is upstream v0.23.0 plus 29 commits. Its only CUDA change (UE8M0 tile-scale rounding in
cache_kernels.cu) is gated to SM100 / VLLM_DS_MLA_UE8M0_SCALE, so on A100 the stock precompiled
kernels are exactly what a full fork build would run. Every base file is hash-checked against the
upstream tag before it is replaced and every fork file is hash-checked after download.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from importlib.metadata import version
from pathlib import Path

RAW = "https://raw.githubusercontent.com/SKT-AI/vllm/{sha}/{path}"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fetch(sha, path):
    for attempt in range(6):
        try:
            with urllib.request.urlopen(RAW.format(sha=sha, path=path), timeout=60) as response:
                return response.read()
        except Exception:
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name("overlay_manifest.json").read_text())
    installed = version("vllm")
    if not installed.startswith("0.23.0"):
        raise SystemExit(f"expected stock vLLM 0.23.0, found {installed}")
    import importlib.util
    site = Path(importlib.util.find_spec("vllm").origin).parent.parent
    result = {"vllm": installed, "site": str(site), "fork_sha": manifest["fork_sha"],
              "base_checked": 0, "already_applied": 0, "written": 0}
    for item in manifest["files"]:
        target = site / item["path"]
        current = sha256(target.read_bytes()) if target.exists() else None
        if current == item["fork_sha256"]:
            result["already_applied"] += 1
            continue
        if item["status"] == "modified" and current != item["base_sha256"]:
            raise SystemExit(f"installed {item['path']} is not the upstream v0.23.0 file")
        if item["status"] == "added" and current is not None:
            raise SystemExit(f"unexpected existing file {item['path']}")
        result["base_checked"] += item["status"] == "modified"
        data = fetch(manifest["fork_sha"], item["path"])
        if sha256(data) != item["fork_sha256"]:
            raise SystemExit(f"fork download hash mismatch for {item['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".axk2tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        result["written"] += 1
    probe = ("from vllm.model_executor.models.registry import ModelRegistry;"
             "from vllm.transformers_utils.configs import AXK2Config;"
             "print('AXK2ForCausalLM' in ModelRegistry.get_supported_archs())")
    check = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=600)
    result["axk2_registered"] = check.stdout.strip().endswith("True")
    result["probe_stderr_tail"] = check.stderr[-1500:]
    Path(args.report).write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
    if not result["axk2_registered"]:
        raise SystemExit("AXK2 architecture not registered after overlay")


if __name__ == "__main__":
    main()
