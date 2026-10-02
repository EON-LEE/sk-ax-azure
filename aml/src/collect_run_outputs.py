"""Return selected files from a finished job's private artifacts through the job record.

Runs as a small CPU job inside the workspace's managed VNet with the finished job's `outputs`
folder as a downloaded input. It re-reports the per-position smoke series and the vLLM start-up
facts that matter as evidence: kernel/backend selection, weight loading and KV-cache sizing.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = re.compile(
    r"marlin|backend|kv cache|maximum concurrency|loading weights took|model loading took|"
    r"weights took|available kv|gpu kv|fp8|quantiz|prefill|mla|pipeline|placement|"
    r"torch.compile|cuda graph|graph capturing|memory profiling|init engine", re.IGNORECASE)
NOISE = re.compile(r"NCCL INFO|jit_monitor|Avg prompt throughput|GET /|POST /")


def report(key, text=None, file=None, series=False):
    command = [sys.executable, str(Path(__file__).with_name("report.py")), key]
    command += ["--file", str(file)] if file else ["--text", text]
    if series:
        command.append("--series")
    subprocess.run(command, check=False)


def facts(log):
    seen, kept = set(), []
    for line in log.read_text(errors="replace").splitlines():
        if not PATTERNS.search(line) or NOISE.search(line):
            continue
        message = re.sub(r"^.*?(INFO|WARNING|ERROR) \d\d-\d\d [\d:]+ ", r"\1 ", line)
        message = re.sub(r"pid=\d+", "pid", message)[:300]
        if message not in seen:
            seen.add(message)
            kept.append(message)
    return "\n".join(kept)


def main():
    root = Path(sys.argv[1])
    found = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    report("collect.files", json.dumps(found[:400]))
    node0 = root / "node0"
    for name in ("series-pp1.json", "series-tp8-pp2.json"):
        path = node0 / name
        if path.exists():
            tag = name[len("series-"):-len(".json")]
            report(f"node0.{tag}", file=path, series=True)
    for log in sorted(node0.glob("vllm-*.log")):
        report(f"facts.{log.stem}", facts(log)[-24000:])


if __name__ == "__main__":
    main()
