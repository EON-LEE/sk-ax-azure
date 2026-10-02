"""Stage the pinned skt/A.X-K2 checkpoint into an Azure ML job output (workspace blob storage).

Runs on a CPU node so the GPU nodes later copy the weights from same-region storage instead of
spending paid GPU time on a 694 GB internet download.
"""
import argparse
import fnmatch
import json
import os
import shutil
import time
from pathlib import Path

REPO = "skt/A.X-K2"
PATTERNS = ["*.safetensors", "*.json", "*.jinja", "README.md", ".gitattributes"]


def wanted(name):
    return "/" not in name and any(fnmatch.fnmatch(name, pattern) for pattern in PATTERNS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from huggingface_hub import HfApi, snapshot_download

    info = HfApi().model_info(REPO, revision=args.revision, files_metadata=True)
    expected = {s.rfilename: s.size for s in info.siblings if wanted(s.rfilename)}
    out = Path(args.out)
    began = time.time()
    snapshot_download(REPO, revision=args.revision, local_dir=out, allow_patterns=PATTERNS,
                      max_workers=args.workers)
    seconds = time.time() - began
    shutil.rmtree(out / ".cache", ignore_errors=True)
    present = {p.name: p.stat().st_size for p in out.iterdir() if p.is_file()}
    missing = sorted(set(expected) - set(present))
    wrong_size = sorted(n for n in expected if n in present and present[n] != expected[n])
    total = sum(present.get(n, 0) for n in expected)
    report = {
        "repo": REPO, "revision": args.revision, "sha_reported_by_hub": info.sha,
        "files_expected": len(expected), "files_present": len(present),
        "missing": missing, "wrong_size": wrong_size, "bytes": total,
        "download_seconds": round(seconds, 1),
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
