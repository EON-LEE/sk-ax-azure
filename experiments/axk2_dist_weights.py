"""Bounded, exact-range extraction of real AXK2 blocks 0/1 and 256 embedding rows."""
import concurrent.futures
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REVISION = "2287ca456927eed33b899c404c0b2ffaf17aa09f"
BASE = f"https://huggingface.co/skt/A.X-K2/resolve/{REVISION}/"
ROOT = Path("/opt/axk2-dist")
DATA = ROOT / "weights"
DTYPES = {"F8_E4M3": "float8_e4m3fn", "F32": "float32", "BF16": "bfloat16"}


def get_json(filename):
    with urlopen(BASE + filename, timeout=120) as response:
        return json.load(response)


def read_range(shard, start, length):
    assert 0 < length <= 192 * 1024**2
    end = start + length - 1
    request = Request(
        BASE + shard + f"?download=true&range_id={start}-{end}",
        headers={"Range": f"bytes={start}-{end}"},
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=180) as response:
                if response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {start}-{end}/"):
                    raise RuntimeError("Exact range not honored; refusing a whole-shard download.")
                data = response.read(length + 1)
            assert len(data) == length
            return data
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            delay = max(float(exc.headers.get("Retry-After", 0)), 10 * (attempt + 1))
            print(f"HTTP {exc.code}; retry after {delay}s", flush=True)
            time.sleep(delay)


def tensor_path(name):
    return DATA / (name + ".bin")


def load_tensor(name, manifest, device="cpu"):
    import torch
    item = manifest["tensors"][name]
    data = tensor_path(name).read_bytes()
    assert len(data) == item["bytes"], name
    assert hashlib.sha256(data).hexdigest() == item["sha256"], name
    dtype = getattr(torch, DTYPES[item["dtype"]])
    tensor = torch.frombuffer(bytearray(data), dtype=dtype).reshape(item["shape"])
    return tensor.to(device=device)


def prepare():
    rank = int(os.environ["RANK"])
    layers = [0] if rank == 0 else [0, 1]  # rank1 also runs a serial reference.
    DATA.mkdir(parents=True, exist_ok=True)
    config = get_json("config.json")
    (ROOT / "model-config.json").write_text(json.dumps(config, indent=2))
    index = get_json("model.safetensors.index.json")["weight_map"]
    selected = {
        name: shard for name, shard in index.items()
        if any(name.startswith(f"model.layers.{layer}.") for layer in layers)
    }
    assert all("model.layers.2." not in name for name in selected)
    embedding_name = "model.embed_tokens.weight"
    shards = sorted(set(selected.values()) | {index[embedding_name]})
    headers = {}
    for shard in shards:
        length = int.from_bytes(read_range(shard, 0, 8), "little")
        assert length < 4 * 1024**2
        headers[shard] = (8 + length, json.loads(read_range(shard, 8, length)))
    jobs = []
    selected_bytes = 0
    for shard in shards:
        base, header = headers[shard]
        entries = []
        for name in selected:
            if selected[name] != shard:
                continue
            metadata = header[name]
            assert metadata["dtype"] in DTYPES
            start, end = metadata["data_offsets"]
            selected_bytes += end - start
            entries.append((start, end, name, metadata))
        entries.sort()
        group = []
        for entry in entries:
            if group and (entry[0] - group[-1][1] > 4096 or entry[1] - group[0][0] > 192 * 1024**2):
                jobs.append((shard, base, group))
                group = []
            group.append(entry)
        if group:
            jobs.append((shard, base, group))
    assert selected_bytes < 16 * 1024**3, "Subset download budget exceeded"
    download_bytes = sum(group[-1][1] - group[0][0] for _, _, group in jobs)
    assert download_bytes < 16 * 1024**3
    manifest = {
        "model_revision": REVISION, "rank": rank, "blocks": layers,
        "scope": "Only complete decoder blocks 0/1 and 256 embedding rows. Not 61-block full model.",
        "selected_tensor_bytes": selected_bytes, "range_download_bytes": download_bytes,
        "tensors": {},
    }
    lock = threading.Lock()

    def download(job):
        shard, base, entries = job
        start, end = entries[0][0], entries[-1][1]
        data = read_range(shard, base + start, end - start)
        completed = {}
        for a, b, name, metadata in entries:
            chunk = data[a - start:b - start]
            path = tensor_path(name)
            path.write_bytes(chunk)
            completed[name] = {
                "shard": shard, "dtype": metadata["dtype"], "shape": metadata["shape"],
                "bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest(),
            }
        with lock:
            manifest["tensors"].update(completed)
            print(f"downloaded {len(manifest['tensors'])}/{len(selected)} tensors", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(download, jobs))
    shard = index[embedding_name]
    base, header = headers[shard]
    meta = header[embedding_name]
    assert meta["dtype"] == "BF16" and meta["shape"] == [163840, 7168]
    row_start, row_count = 20000, 256
    data = read_range(shard, base + meta["data_offsets"][0] + row_start * 7168 * 2, row_count * 7168 * 2)
    tensor_path("embedding_subset").write_bytes(data)
    manifest["embedding_token_start"] = row_start
    manifest["tensors"]["embedding_subset"] = {
        "shard": shard, "dtype": "BF16", "shape": [row_count, 7168], "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    assert len(manifest["tensors"]) == len(selected) + 1
    (ROOT / "weights-manifest.json").write_text(json.dumps(manifest, indent=2))
    summary = {key: value for key, value in manifest.items() if key != "tensors"}
    summary["tensor_count"] = len(manifest["tensors"])
    summary["manifest_sha256"] = hashlib.sha256((ROOT / "weights-manifest.json").read_bytes()).hexdigest()
    (ROOT / "download-summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    prepare()
