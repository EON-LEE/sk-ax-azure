"""Fetch only one public FP8 expert by validated HTTP ranges; no full model."""
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

import torch
from transformers.integrations.finegrained_fp8 import Fp8Dequantize
from axk2_cuda_smoke import difference

REV = "2287ca456927eed33b899c404c0b2ffaf17aa09f"
SHARD = "model-00001-of-00346.safetensors"
BASE = f"https://huggingface.co/skt/A.X-K2/resolve/{REV}/{SHARD}"
PREFIX = "model.layers.1.mlp.experts.0."
total_downloaded = 0


def read_range(start, length):
    global total_downloaded
    assert 0 < length < 64 * 1024**2
    assert total_downloaded + length < 128 * 1024**2
    end = start + length - 1
    request = Request(BASE + f"?download=true&range_id={start}-{end}",
                      headers={"Range": f"bytes={start}-{end}"})
    with urlopen(request, timeout=120) as response:
        if response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {start}-{end}/"):
            raise RuntimeError("Server did not honor exact range; refusing a full shard download.")
        data = response.read(length + 1)
    assert len(data) == length
    total_downloaded += length
    return data


torch.set_num_threads(4)
torch.manual_seed(42)
torch.backends.cuda.matmul.allow_tf32 = False
assert torch.cuda.get_device_capability() == (8, 0)
header_length = int.from_bytes(read_range(0, 8), "little")
assert header_length < 2 * 1024**2
header = json.loads(read_range(8, header_length))
dtypes = {"F8_E4M3": torch.float8_e4m3fn, "F32": torch.float32,
          "BF16": torch.bfloat16, "U8": torch.uint8}
raw, manifest = {}, {}
for projection in ("gate_proj", "up_proj", "down_proj"):
    for suffix in ("weight", "weight_scale_inv"):
        key = PREFIX + projection + "." + suffix
        metadata = header[key]
        offset, end = metadata["data_offsets"]
        data = read_range(8 + header_length + offset, end - offset)
        raw[projection + "." + suffix] = torch.frombuffer(bytearray(data), dtype=dtypes[metadata["dtype"]]).clone().reshape(metadata["shape"])
        manifest[key] = {"shape": metadata["shape"], "dtype": metadata["dtype"], "sha256": hashlib.sha256(data).hexdigest()}

dequantizer = Fp8Dequantize(None)
cpu_weights = {
    name: dequantizer._dequantize_one(raw[name + ".weight"], raw[name + ".weight_scale_inv"], torch.float32)
    for name in ("gate_proj", "up_proj", "down_proj")
}
gpu_raw = {key: tensor.cuda() for key, tensor in raw.items()}
inputs = torch.randn(32, 7168)


def apply_cpu(x):
    gate = torch.nn.functional.linear(x, cpu_weights["gate_proj"])
    up = torch.nn.functional.linear(x, cpu_weights["up_proj"])
    return torch.nn.functional.linear(torch.nn.functional.silu(gate) * up, cpu_weights["down_proj"])


def gpu_projection(name, x):
    weight = dequantizer._dequantize_one(
        gpu_raw[name + ".weight"], gpu_raw[name + ".weight_scale_inv"], torch.float32,
    )
    return torch.nn.functional.linear(x, weight)


with torch.inference_mode():
    reference = apply_cpu(inputs)
    x = inputs.cuda()
    gate, up = gpu_projection("gate_proj", x), gpu_projection("up_proj", x)
    output = gpu_projection("down_proj", torch.nn.functional.silu(gate) * up)
    comparison = difference(output.cpu(), reference, 1e-4, 1e-4, enforce=False)
    gpu_weights = {
        name: dequantizer._dequantize_one(gpu_raw[name + ".weight"], gpu_raw[name + ".weight_scale_inv"], torch.float32)
        for name in ("gate_proj", "up_proj", "down_proj")
    }
    materialized = torch.nn.functional.linear(
        torch.nn.functional.silu(torch.nn.functional.linear(x, gpu_weights["gate_proj"])) *
        torch.nn.functional.linear(x, gpu_weights["up_proj"]), gpu_weights["down_proj"],
    )
    equivalence = difference(output, materialized, 0, 0)

result = {
    "scope": "Actual public checkpoint: layer1 expert0 only; 32 random input activations. Not a complete layer/model, text generation, native FP8 GEMM or quality test.",
    "model_revision": REV, "shard": SHARD, "manifest": manifest,
    "downloaded_bytes": total_downloaded, "gpu": torch.cuda.get_device_name(),
    "compute": "FP8 weights stay stored; official pinned Transformers converter expands each projection to FP32 for A100 GEMM.",
    "stored_weight_and_scale_bytes": sum(t.numel() * t.element_size() for t in gpu_raw.values()),
    "all_fp32_weight_bytes": sum(t.numel() * t.element_size() for t in cpu_weights.values()),
    "cpu_vs_cuda": comparison, "on_demand_vs_materialized_cuda": equivalence,
    "finite": bool(torch.isfinite(output).all()), "passed": comparison["within_tolerance"],
}
Path("/tmp/axk2-checkpoint-results.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
if not result["passed"]:
    raise AssertionError("Real-weight CPU/GPU comparison failed.")
