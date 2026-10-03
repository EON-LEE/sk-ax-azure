"""Measure Hugging Face download throughput from inside the workspace network (two real shards)."""
import json
import os
import sys
import time
import urllib.request

from huggingface_hub import hf_hub_download

REV = "2287ca456927eed33b899c404c0b2ffaf17aa09f"


def main():
    out = {}
    began = time.time()
    try:
        with urllib.request.urlopen(f"https://huggingface.co/skt/A.X-K2/resolve/{REV}/config.json", timeout=30) as r:
            out["resolve_status"] = r.status
    except Exception as exc:
        out["resolve_error"] = str(exc)[:300]
    out["resolve_seconds"] = round(time.time() - began, 2)
    for shard in ("model-00100-of-00346.safetensors", "model-00200-of-00346.safetensors"):
        began = time.time()
        try:
            path = hf_hub_download("skt/A.X-K2", shard, revision=REV, local_dir="/tmp/probe")
            seconds = time.time() - began
            out[shard] = {"MBps": round(os.path.getsize(path) / seconds / 1e6, 1), "seconds": round(seconds, 1)}
        except Exception as exc:
            out[shard] = {"error": str(exc)[:300]}
    json.dump(out, open(sys.argv[1], "w"))
    print(json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
