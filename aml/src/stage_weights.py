"""Stage a pinned A.X-K2 checkpoint (default skt/A.X-K2; --repo skt/A.X-K2-NVFP4 for the official
NVFP4 variant) into a local folder or an Azure ML job output.

Downloads run in a child process under a watchdog: hf_xet transfers were observed to stall forever
part-way (68% of the files, no progress for 50 minutes) on Azure ML nodes. When the folder stops
growing for --stall-seconds the child is killed and restarted; snapshot_download skips files that
are already complete, so every restart resumes. Progress lines go to stdout.
"""
import argparse
import fnmatch
import json
import multiprocessing
import os
import shutil
import time
from pathlib import Path

PATTERNS = ["*.safetensors", "*.json", "*.jinja", "README.md", ".gitattributes"]


def wanted(name):
    return "/" not in name and any(fnmatch.fnmatch(name, pattern) for pattern in PATTERNS)


def tree_bytes(*roots):
    total = 0
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, filename))
                except OSError:
                    pass
    return total


def _download(repo, revision, out, workers):
    from huggingface_hub import snapshot_download
    snapshot_download(repo, revision=revision, local_dir=out, allow_patterns=PATTERNS, max_workers=workers)


def download_with_watchdog(repo, revision, out, workers, stall_seconds, attempts, expected_bytes=0):
    cache = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    of = f" of {expected_bytes / 1e9:.1f} GB" if expected_bytes else ""
    for attempt in range(1, attempts + 1):
        child = multiprocessing.get_context("spawn").Process(target=_download,
                                                             args=(repo, revision, str(out), workers))
        child.start()
        last_size, last_change, last_print = -1, time.time(), 0.0
        while child.is_alive():
            child.join(timeout=20)
            size = tree_bytes(out, cache)
            now = time.time()
            if size != last_size:
                last_size, last_change = size, now
            if now - last_print > 60:
                print(f"[download] attempt {attempt}: {size / 1e9:.1f} GB on disk{of}", flush=True)
                last_print = now
            if child.is_alive() and now - last_change > stall_seconds:
                print(f"[download] no progress for {stall_seconds}s at {size / 1e9:.1f} GB; restarting", flush=True)
                child.terminate()
                child.join(30)
                if child.is_alive():
                    child.kill()
                    child.join()
                break
        if child.exitcode == 0:
            return attempt
    raise SystemExit(f"download did not finish after {attempts} attempts")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="skt/A.X-K2")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--stall-seconds", type=int, default=180)
    parser.add_argument("--attempts", type=int, default=10)
    args = parser.parse_args()
    os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from huggingface_hub import HfApi

    info = HfApi().model_info(args.repo, revision=args.revision, files_metadata=True)
    expected = {s.rfilename: s.size for s in info.siblings if wanted(s.rfilename)}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    began = time.time()
    attempts = download_with_watchdog(args.repo, args.revision, out, args.workers, args.stall_seconds,
                                      args.attempts, sum(size or 0 for size in expected.values()))
    seconds = time.time() - began
    shutil.rmtree(out / ".cache", ignore_errors=True)
    present = {p.name: p.stat().st_size for p in out.iterdir() if p.is_file()}
    missing = sorted(set(expected) - set(present))
    wrong_size = sorted(n for n in expected if n in present and present[n] != expected[n])
    total = sum(present.get(n, 0) for n in expected)
    report = {
        "repo": args.repo, "revision": args.revision, "sha_reported_by_hub": info.sha,
        "files_expected": len(expected), "files_present": len(present),
        "missing": missing, "wrong_size": wrong_size, "bytes": total,
        "download_seconds": round(seconds, 1), "download_attempts": attempts,
        "throughput_MBps": round(total / max(seconds, 1e-9) / 1e6, 1),
        "integrity": "File sizes match the Hub listing; hf_xet verifies content-addressed chunk hashes.",
        "passed": not missing and not wrong_size and info.sha == args.revision,
    }
    (out / "STAGED_MANIFEST.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    if not report["passed"]:
        raise SystemExit("staging verification failed")


if __name__ == "__main__":
    main()
