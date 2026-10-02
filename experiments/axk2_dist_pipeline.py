"""Real checkpoint blocks 0 -> 1 over NCCL/TCP on two separate A100 VMs.

This is a two-block correctness experiment, not a full language model or API.
"""
import gc
import hashlib
import json
import os
import socket
import time
import traceback
from datetime import timedelta
from pathlib import Path

import torch
import torch.distributed as dist
import transformers
from transformers import AXK2Config, DynamicCache
from transformers.integrations.finegrained_fp8 import Fp8Dequantize
from transformers.models.axk2.modeling_axk2 import AXK2DecoderLayer, AXK2RotaryEmbedding
from axk2_dist_weights import ROOT, REVISION, load_tensor

RANK = int(os.environ["RANK"])
MANIFEST = json.loads((ROOT / "weights-manifest.json").read_text())
DEQUANT = Fp8Dequantize(None)
USED = set()


def checkpoint_dequantize(weight, scales):
    """Use checkpoint block size, including partially populated edge blocks."""
    assert weight.ndim == scales.ndim == 2
    rows, cols = weight.shape
    assert scales.shape == ((rows + 127) // 128, (cols + 127) // 128), (weight.shape, scales.shape)
    if rows % 128 == 0 and cols % 128 == 0:
        return DEQUANT._dequantize_one(weight, scales, torch.float32)
    expanded_scales = scales.float().repeat_interleave(128, 0).repeat_interleave(128, 1)
    return weight.float() * expanded_scales[:rows, :cols]


def validate_edge_blocks():
    torch.manual_seed(3)
    weight = torch.randn(576, 256).to(torch.float8_e4m3fn)
    scales = torch.rand(5, 2)
    expected = torch.empty(576, 256)
    for row in range(5):
        for col in range(2):
            region = (slice(row * 128, min((row + 1) * 128, 576)),
                      slice(col * 128, (col + 1) * 128))
            expected[region] = weight[region].float() * scales[row, col]
    actual = checkpoint_dequantize(weight, scales)
    torch.testing.assert_close(actual, expected, atol=0, rtol=0)
    aligned = checkpoint_dequantize(weight[:512], scales[:4])
    torch.testing.assert_close(aligned, expected[:512], atol=0, rtol=0)
    gpu = checkpoint_dequantize(weight.cuda(), scales.cuda()).cpu()
    torch.testing.assert_close(gpu, expected, atol=0, rtol=0)
    return {"tail_shape": [576, 256], "scale_grid": [5, 2],
            "cpu_tile_reference_max_error": 0.0, "gpu_tile_reference_max_error": 0.0}


def get(name, device="cpu"):
    tensor = load_tensor(name, MANIFEST, device)
    USED.add(name)
    return tensor


def expanded_weight(name, device="cuda"):
    tensor = get(name)
    scale_name = name + "_scale_inv"
    if tensor.dtype == torch.float8_e4m3fn:
        tensor = checkpoint_dequantize(tensor, get(scale_name))
    else:
        tensor = tensor.float()
    return tensor.to(device)


class StoredFP8Experts(torch.nn.Module):
    """Original expert weights; FP32 projection expansion only on actual use."""
    def __init__(self, layer):
        super().__init__()
        self.expert_count = 256
        for expert in range(256):
            for short, projection in (("g", "gate_proj"), ("u", "up_proj"), ("d", "down_proj")):
                name = f"model.layers.{layer}.mlp.experts.{expert}.{projection}.weight"
                self.register_buffer(f"{short}{expert}", get(name, "cuda"))
                self.register_buffer(f"{short}{expert}s", get(name + "_scale_inv", "cuda"))
        self.experts_used = set()

    def projection(self, short, expert, inputs):
        weight = checkpoint_dequantize(getattr(self, f"{short}{expert}"), getattr(self, f"{short}{expert}s"))
        return torch.nn.functional.linear(inputs, weight)

    def forward(self, hidden_states, top_k_index, top_k_weights):
        assert hidden_states.dtype == torch.float32
        output = torch.zeros_like(hidden_states)
        for expert in top_k_index.unique(sorted=True).tolist():
            assert 0 <= expert < self.expert_count
            self.experts_used.add(expert)
            slot, token = torch.where((top_k_index == expert).transpose(0, 1))
            inputs = hidden_states[token]
            gate = self.projection("g", expert, inputs)
            up = self.projection("u", expert, inputs)
            values = self.projection("d", expert, torch.nn.functional.silu(gate) * up)
            output.index_add_(0, token, values * top_k_weights[token, slot, None])
        return output


def load_block(config, index):
    with torch.device("meta"):
        block = AXK2DecoderLayer(config, index)
    if index == 1:
        block.mlp.experts = StoredFP8Experts(index)
    for name, parameter in list(block.named_parameters()):
        source = f"model.layers.{index}." + name
        source = source.replace(".mlp.fc1.", ".W_down.").replace(".mlp.fc2.", ".W_up.")
        source = source.replace(".self_attn.q_gate_proj.", ".self_attn.q_b_proj.")
        value = expanded_weight(source)
        assert tuple(value.shape) == tuple(parameter.shape), (source, value.shape, parameter.shape)
        parent, _, leaf = name.rpartition(".")
        module = block.get_submodule(parent) if parent else block
        module.register_parameter(leaf, torch.nn.Parameter(value, requires_grad=False))
    for name, buffer in list(block.named_buffers()):
        if not buffer.is_meta:
            continue
        value = expanded_weight(f"model.layers.{index}." + name)
        assert tuple(value.shape) == tuple(buffer.shape), (name, value.shape, buffer.shape)
        parent, _, leaf = name.rpartition(".")
        module = block.get_submodule(parent) if parent else block
        module.register_buffer(leaf, value)
    expected = {name for name in MANIFEST["tensors"] if name.startswith(f"model.layers.{index}.")}
    assert expected <= USED, sorted(expected - USED)
    assert not any(t.is_meta for t in (*block.parameters(), *block.buffers()))
    block.eval()
    return block


def block_fingerprint(index):
    tensors = [(name, item["sha256"]) for name, item in sorted(MANIFEST["tensors"].items())
               if name.startswith(f"model.layers.{index}.")]
    return hashlib.sha256(json.dumps(tensors).encode()).hexdigest()


def comparison(actual, reference):
    actual, reference = actual.float().cpu(), reference.float().cpu()
    delta = actual - reference
    return {
        "max_absolute_error": delta.abs().max().item(),
        "relative_l2_error": (delta.norm() / reference.norm().clamp_min(1e-12)).item(),
        "atol": 1e-4, "rtol": 1e-4,
        "passed": bool(torch.allclose(actual, reference, atol=1e-4, rtol=1e-4)),
        "finite": bool(torch.isfinite(actual).all()),
    }


def forward_block(block, rotary, inputs, start, cache):
    length = inputs.shape[1]
    positions = torch.arange(start, start + length, device="cuda").unsqueeze(0)
    keys = torch.arange(start + length, device="cuda")
    allowed = keys[None, :] <= positions[0, :, None]
    mask = torch.zeros((1, 1, length, start + length), device="cuda", dtype=torch.float32)
    mask.masked_fill_(~allowed[None, None], torch.finfo(torch.float32).min)
    output = block(
        hidden_states=inputs,
        position_embeddings=rotary(inputs, positions),
        attention_mask=mask, past_key_values=cache, position_ids=positions, use_cache=cache is not None,
    )
    assert torch.isfinite(output).all(), "Nonfinite real-block output"
    return output


def save(result):
    (ROOT / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)


def main():
    torch.set_num_threads(8)
    torch.cuda.set_device(0)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    assert torch.cuda.get_device_capability() == (8, 0)
    config_data = json.loads((ROOT / "model-config.json").read_text())
    assert config_data["quantization_config"]["weight_block_size"] == [128, 128]
    edge_check = validate_edge_blocks()
    config_data.pop("quantization_config", None)
    config = AXK2Config.from_dict(config_data)
    config._attn_implementation = "eager"
    rotary = AXK2RotaryEmbedding(config=config).cuda()
    embedding = get("embedding_subset").float().cuda()
    prompt = json.loads((ROOT / "prompt-data.json").read_text())
    prompt_bytes = (ROOT / "prompt-embeddings.bin").read_bytes()
    assert hashlib.sha256(prompt_bytes).hexdigest() == prompt["embedding_sha256"]
    prompt_embedding = torch.frombuffer(bytearray(prompt_bytes), dtype=torch.bfloat16).reshape(prompt["shape"]).float().cuda()
    block = load_block(config, RANK)
    stage_bytes = sum(t.numel() * t.element_size() for t in (*block.parameters(), *block.buffers()))
    reference_block0 = load_block(config, 0) if RANK == 1 else None
    result = {
        "scope": "Real pretrained decoder blocks 0 and 1 only, not the full 61-block model or response generation.",
        "model_revision": REVISION,
        "transformers_revision": "7fb5bcd1d4b8a5c225a2c33429b2e9e023dd61ae",
        "torch": torch.__version__, "transformers": transformers.__version__,
        "rank": RANK, "hostname": socket.gethostname(), "gpu": torch.cuda.get_device_name(),
        "pipeline_stage": f"decoder block {RANK}",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "stage_checkpoint_fingerprint": block_fingerprint(RANK),
        "memory_scope": "Includes validation allocations and rank1 serial-reference block0; not full-model serving memory.",
        "timing_scope": "Sequential two-stage validation including synchronization and output copies; not throughput benchmark.",
        "prompt_embedding_sha256": prompt["embedding_sha256"],
        "prompt_text_sha256": prompt["text_sha256"],
        "stage_resident_weight_bytes": stage_bytes,
        "transport": "NCCL over private VNet TCP; no RDMA on this SKU",
        "reference": "Rank1 separately evaluates both identical blocks serially on its one GPU.",
        "checkpoint_manifest_sha256": hashlib.sha256((ROOT / "weights-manifest.json").read_bytes()).hexdigest(),
        "loader_edge_block_check": edge_check,
        "loader_patch": "Original 128x128 block scales with a truncated final block; required by actual kv_a_proj_with_mqa shape576x7168/scales5x56.",
        "cases": [], "passed": False,
    }
    save(result)
    dist.init_process_group(
        "nccl", rank=RANK, world_size=2, timeout=timedelta(minutes=8),
        device_id=torch.device("cuda:0"),
    )
    peers = [None, None]
    dist.all_gather_object(peers, {"rank": RANK, "hostname": socket.gethostname(),
                                  "stage": RANK, "weight_bytes": stage_bytes,
                                  "checkpoint": block_fingerprint(RANK),
                                  "script": result["script_sha256"],
                                  "prompt": prompt["embedding_sha256"],
                                  "random_embedding": MANIFEST["tensors"]["embedding_subset"]["sha256"]})
    assert peers[0]["hostname"] != peers[1]["hostname"], "Must be two separate VMs"
    assert peers[0]["script"] == peers[1]["script"]
    assert peers[0]["prompt"] == peers[1]["prompt"]
    assert peers[0]["random_embedding"] == peers[1]["random_embedding"]
    if RANK == 1:
        assert peers[0]["checkpoint"] == block_fingerprint(0), "Serial reference must use identical stage0 weights"
    result["peers"] = peers
    handshake = torch.ones(1, device="cuda")
    with torch.inference_mode():
        cases = [
            ("random", 256, 8, 1490), ("random", 4096, 32, 5330),
            ("random", 4096, 32, 2026), ("random", 8192, 32, 9426),
            ("bilingual", 256, 8, 0), ("bilingual", 4096, 32, 0),
            ("bilingual", 8192, 32, 0),
        ]
        for kind, sequence, decode, seed in cases:
            result["running_case"] = {"input": kind, "tokens": sequence, "seed": seed}
            save(result)
            if kind == "random":
                torch.manual_seed(seed)
                token_offsets = torch.randint(0, 256, (1, sequence), device="cpu").cuda()
                inputs = embedding[token_offsets]
            else:
                offsets = prompt["embedding_offsets"]
                repeated = offsets * ((sequence + len(offsets) - 1) // len(offsets))
                token_offsets = torch.tensor(repeated[:sequence])
                inputs = prompt_embedding[token_offsets.cuda().unsqueeze(0)]
            prefix_length = sequence - decode
            chunks = [(0, prefix_length)] + [(i, 1) for i in range(prefix_length, sequence)]
            references = []
            serial_full_tail = None
            if RANK == 1:
                serial_cache = DynamicCache(config=config)
                for start, length in chunks:
                    intermediate = forward_block(reference_block0, rotary, inputs[:, start:start + length], start, serial_cache)
                    expected = forward_block(block, rotary, intermediate, start, serial_cache)
                    references.append(expected.cpu())
                del serial_cache, intermediate, expected
                full0 = forward_block(reference_block0, rotary, inputs, 0, None)
                full1 = forward_block(block, rotary, full0, 0, None)
                serial_full_tail = full1[:, -decode:].cpu()
                del full0, full1
                gc.collect()
                torch.cuda.empty_cache()
            dist.barrier()
            if RANK == 1:
                block.mlp.experts.experts_used.clear()
            cache = DynamicCache(config=config)
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
            begin = time.perf_counter()
            outputs, phases = [], []
            for step, (start, length) in enumerate(chunks):
                phase_start = time.perf_counter()
                if RANK == 0:
                    intermediate = forward_block(block, rotary, inputs[:, start:start + length], start, cache).contiguous()
                    dist.send(intermediate, dst=1)
                    dist.recv(handshake, src=1)
                    del intermediate
                else:
                    incoming = torch.empty((1, length, 7168), device="cuda", dtype=torch.float32)
                    dist.recv(incoming, src=0)
                    output = forward_block(block, rotary, incoming, start, cache)
                    torch.cuda.synchronize()
                    dist.send(handshake, dst=0)
                    outputs.append(output.cpu())
                    del incoming, output
                torch.cuda.synchronize()
                phases.append(time.perf_counter() - phase_start)
            elapsed = time.perf_counter() - begin
            case = {
                "input_kind": kind, "seed": seed,
                "input_note": "Real tokenizer/embedding rows of repeated synthetic bilingual text." if kind == "bilingual"
                              else "Random token sequence from actual pretrained embedding rows20000:20256.",
                "total_tokens": sequence, "prefill_tokens": prefix_length, "decode_tokens": decode,
                "activation_bytes_sent": sequence * 7168 * 4,
                "end_to_end_seconds": elapsed, "prefill_seconds": phases[0],
                "decode_seconds": sum(phases[1:]),
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
            }
            if RANK == 1:
                case["distributed_vs_serial_prefill"] = comparison(outputs[0], references[0])
                decoded = torch.cat(outputs[1:], dim=1)
                reference_decoded = torch.cat(references[1:], dim=1)
                case["distributed_vs_serial_decode"] = comparison(decoded, reference_decoded)
                case["full_vs_cached_serial_control"] = comparison(reference_decoded, serial_full_tail)
                case["routed_experts_observed"] = len(block.mlp.experts.experts_used)
                case["passed"] = all(case[key]["passed"] and case[key]["finite"] for key in (
                    "distributed_vs_serial_prefill", "distributed_vs_serial_decode",
                ))
            result["cases"].append(case)
            save(result)
            del cache, inputs, outputs, references
            gc.collect()
            torch.cuda.empty_cache()
            dist.barrier()
    if RANK == 1:
        result["passed"] = all(case["passed"] for case in result["cases"])
        result["full_vs_cached_serial_all_passed"] = all(
            case["full_vs_cached_serial_control"]["passed"] for case in result["cases"])
    else:
        result["passed"] = True
        result["passed_scope"] = "Transport and stage0 completed; rank1 is authoritative for numerical parity."
    result.pop("running_case", None)
    save(result)
    dist.destroy_process_group()
    if not result["passed"]:
        raise AssertionError("Distributed-vs-serial numerical parity failed")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        (ROOT / "failure.json").write_text(json.dumps({
            "rank": RANK, "type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc(),
        }, indent=2))
        raise
