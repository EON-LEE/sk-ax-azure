"""Cut a 2-layer smoke checkpoint out of the real A.X-K2 weights.

Keeps the real embedding, decoder layer 0 (dense MLP), decoder layer 1 (256-expert MoE), final
norm and lm_head, byte-for-byte from the published FP8 shards. The same file then exercises every
kernel family the full model uses on A100 (FP8 Marlin linear + MoE, MLA, gated norms, router,
pipeline-parallel transfer) at 1/30 of the size, so the multi-node launch can be rehearsed cheaply.
"""
import argparse
import hashlib
import json
import random
import shutil
import struct
import time
from pathlib import Path

REPO = "skt/A.X-K2"
KEEP_LAYERS = (0, 1)
SIDE_FILES = ["config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
              "special_tokens_map.json", "chat_template.jinja"]


def keep(name):
    if name.startswith("model.layers."):
        return int(name.split(".")[2]) in KEEP_LAYERS
    return True


def group(name):
    if name.startswith("model.layers."):
        return f"layer{name.split('.')[2]}"
    return "embed" if name.startswith("model.embed_tokens") else "head"


def read_header(path):
    with open(path, "rb") as handle:
        size = struct.unpack("<Q", handle.read(8))[0]
        return 8 + size, json.loads(handle.read(size))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(64 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_safetensors(path, entries):
    header, offset = {}, 0
    for name, meta, _, _ in entries:
        nbytes = meta["data_offsets"][1] - meta["data_offsets"][0]
        header[name] = {"dtype": meta["dtype"], "shape": meta["shape"],
                        "data_offsets": [offset, offset + nbytes]}
        offset += nbytes
    header["__metadata__"] = {"format": "pt"}
    blob = json.dumps(header, separators=(",", ":")).encode()
    blob += b" " * ((8 - len(blob) % 8) % 8)
    tensor_hashes = {}
    with open(path, "wb") as out:
        out.write(struct.pack("<Q", len(blob)))
        out.write(blob)
        for name, meta, source, data_start in entries:
            begin, end = meta["data_offsets"]
            digest = hashlib.sha256()
            with open(source, "rb") as handle:
                handle.seek(data_start + begin)
                remaining = end - begin
                while remaining:
                    chunk = handle.read(min(remaining, 64 << 20))
                    digest.update(chunk)
                    out.write(chunk)
                    remaining -= len(chunk)
            tensor_hashes[name] = digest.hexdigest()
    return offset, tensor_hashes


def synthetic_text(rng, sentences, words):
    picked = []
    while len(picked) < words:
        picked.extend(rng.choice(sentences).split())
    return " ".join(picked[:words])


def make_prompts(tokenizer_path):
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    rng = random.Random(2026)
    sentences = [
        "서울은 대한민국의 수도이며 한강을 따라 발전한 도시입니다.",
        "인공지능 모델은 여러 GPU에 나누어 배치하여 추론할 수 있습니다.",
        "파이프라인 병렬화는 레이어를 단계별로 나누어 서로 다른 노드에서 실행합니다.",
        "텐서 병렬화는 하나의 행렬 곱을 여러 GPU가 나누어 계산합니다.",
        "Large language models are served with continuous batching and paged attention.",
        "Pipeline parallelism splits the layers of a model into stages on different nodes.",
        "Tensor parallelism shards each weight matrix across the GPUs inside a node.",
        "The quick brown fox jumps over the lazy dog near the river bank.",
        "def fibonacci(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a",
        "Mixture-of-experts layers route every token to a small subset of expert networks.",
    ]
    specs = [
        ("ko_short", "대한민국의 수도는 어디이며, 그 도시의 특징을 간단히 설명해 주세요."),
        ("en_short", "Explain in two sentences why mixture-of-experts models are efficient to serve."),
        ("code", "def is_prime(n):\n    \"\"\"Return True if n is a prime number.\"\"\"\n"),
        ("mixed_1500", synthetic_text(rng, sentences, 900)),
        ("mixed_2600", synthetic_text(rng, sentences, 1600)),
    ]
    prompts = []
    for name, text in specs:
        ids = tokenizer.encode(text, add_special_tokens=False).ids
        prompts.append({"name": name, "token_ids": ids, "num_tokens": len(ids),
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
    return prompts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--work", default="/tmp/axk2-src")
    args = parser.parse_args()
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download

    out, work = Path(args.out), Path(args.work)
    out.mkdir(parents=True, exist_ok=True)
    index_path = hf_hub_download(REPO, "model.safetensors.index.json", revision=args.revision)
    weight_map = json.loads(Path(index_path).read_text())["weight_map"]
    names = sorted(n for n in weight_map if keep(n))
    shards = sorted({weight_map[n] for n in names})
    info = HfApi().model_info(REPO, revision=args.revision, files_metadata=True)
    lfs = {s.rfilename: s.lfs.sha256 for s in info.siblings if s.lfs}
    began = time.time()
    snapshot_download(REPO, revision=args.revision, local_dir=work, allow_patterns=shards + SIDE_FILES,
                      max_workers=16)
    download_seconds = time.time() - began
    shard_hashes = {}
    for shard in shards:
        shard_hashes[shard] = sha256_file(work / shard)
        if shard_hashes[shard] != lfs[shard]:
            raise SystemExit(f"sha256 mismatch for {shard}")
    headers = {shard: read_header(work / shard) for shard in shards}
    groups = {}
    for name in names:
        shard = weight_map[name]
        data_start, header = headers[shard]
        groups.setdefault(group(name), []).append((name, header[name], work / shard, data_start))
    order = ["embed", *[f"layer{i}" for i in KEEP_LAYERS], "head"]
    total, new_map, tensor_hashes, files = 0, {}, {}, {}
    for number, key in enumerate(order, start=1):
        filename = f"model-{number:05d}-of-{len(order):05d}.safetensors"
        nbytes, hashes = write_safetensors(out / filename, groups[key])
        total += nbytes
        tensor_hashes.update(hashes)
        files[filename] = {"group": key, "tensors": len(groups[key]), "bytes": nbytes}
        new_map.update({name: filename for name, _, _, _ in groups[key]})
    (out / "model.safetensors.index.json").write_text(json.dumps(
        {"metadata": {"total_size": total}, "weight_map": dict(sorted(new_map.items()))}, indent=2))
    for side in SIDE_FILES:
        shutil.copyfile(work / side, out / side)
    config = json.loads((work / "config.json").read_text())
    config["num_hidden_layers"] = len(KEEP_LAYERS)
    (out / "config.json").write_text(json.dumps(config, indent=2))
    prompts = make_prompts(work / "tokenizer.json")
    (out / "smoke_prompts.json").write_text(json.dumps(prompts))
    manifest = {
        "scope": "Real A.X-K2 weights for embedding, decoder layers 0-1, final norm and lm_head only.",
        "repo": REPO, "revision": args.revision, "kept_layers": list(KEEP_LAYERS),
        "config_change": {"num_hidden_layers": [61, len(KEEP_LAYERS)]},
        "source_shards_sha256_verified_against_hub_lfs": shard_hashes,
        "source_download_seconds": round(download_seconds, 1),
        "output_files": files, "tensors": len(names), "bytes": total,
        "tensor_sha256": tensor_hashes,
        "prompts": [{k: v for k, v in p.items() if k != "token_ids"} for p in prompts],
    }
    (out / "SMOKE_MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({k: v for k, v in manifest.items() if k != "tensor_sha256"}), flush=True)


if __name__ == "__main__":
    main()
