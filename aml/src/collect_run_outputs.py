"""Return selected files from a finished job's private artifacts through the job record.

Runs as a small CPU job inside the workspace's managed VNet with the finished job's `outputs`
folder as a downloaded input. It reports the vLLM start-up facts that matter as evidence
(kernel/backend selection, weight loading, KV-cache sizing), the kernel-test log, the phase list
and the benchmark files of the last attention mode (bench files are overwritten per mode).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = re.compile(
    r"marlin|backend|kv cache|maximum concurrency|loading weights took|model loading took|"
    r"weights took|available kv|gpu kv|fp8|quantiz|prefill|mla|pipeline|placement|"
    r"torch.compile|cuda graph|graph capturing|memory profiling|init engine|sparse|indexer|deepgemm", re.IGNORECASE)
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
    for log in sorted(node0.glob("vllm-*.log")):
        report(f"facts.{log.stem}", facts(log)[-24000:])
    for name in ("kernel-tests.log", "phases.jsonl"):
        path = node0 / name
        if path.exists():
            report(f"file.{path.stem}", path.read_text(errors="replace")[-12000:])
    benches = {}
    for path in sorted(node0.glob("bench-*.json")):
        data = json.loads(path.read_text())
        benches[path.stem] = {k: data.get(k) for k in ("completed", "output_throughput", "total_token_throughput",
                                                          "median_ttft_ms", "median_tpot_ms", "median_itl_ms")}
    if benches:
        report("file.bench_last_mode", json.dumps(benches))


if __name__ == "__main__":
    main()
