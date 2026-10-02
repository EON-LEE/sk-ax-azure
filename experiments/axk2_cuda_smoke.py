"""Single-A100 operator/architecture smoke test; never loads public 688B weights."""

import copy
import gc
import json
import platform
import subprocess
import time
from pathlib import Path
from types import MethodType

import torch
import transformers
from transformers import AXK2Config, AXK2ForCausalLM

REVISION = "7fb5bcd1d4b8a5c225a2c33429b2e9e023dd61ae"
OUTPUT = Path("/tmp/axk2-cuda-results.json")


class FP32MLPWithBF16Storage(torch.nn.Module):
    """Correctness-first prototype: expand only the current dense/shared MLP."""

    def __init__(self, inner):
        super().__init__()
        self.inner = inner.to(torch.bfloat16)

    def forward(self, hidden_states):
        parameters = {name: value.float() for name, value in self.inner.named_parameters()}
        return torch.func.functional_call(self.inner, parameters, (hidden_states.float(),))


class FP32ExpertsWithBF16Storage(torch.nn.Module):
    """Expand one selected expert at a time, not the entire expert bank."""

    def __init__(self, inner):
        super().__init__()
        self.inner = inner.to(torch.bfloat16)

    def forward(self, hidden_states, top_k_index, top_k_weights):
        output = torch.zeros_like(hidden_states, dtype=torch.float32)
        for expert in top_k_index.unique(sorted=True):
            slot, token = torch.where((top_k_index == expert).transpose(0, 1))
            gate, up = torch.nn.functional.linear(
                hidden_states[token].float(), self.inner.gate_up_proj[expert].float(),
            ).chunk(2, dim=-1)
            values = torch.nn.functional.linear(
                self.inner.act_fn(gate) * up, self.inner.down_proj[expert].float(),
            )
            output.index_add_(0, token, values * top_k_weights[token, slot, None].float())
        return output


def write_result(result):
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


def config(real_attention=False):
    values = dict(
        vocab_size=128, hidden_size=32, intermediate_size=128, moe_intermediate_size=128,
        num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=4,
        n_shared_experts=1, n_routed_experts=4, n_group=2, topk_group=1,
        num_experts_per_tok=2, kv_lora_rank=16, q_lora_rank=16,
        qk_rope_head_dim=8, qk_nope_head_dim=8, v_head_dim=8,
        index_topk=4, index_head_dim=16, index_n_heads=4, gated_norm_rank=4,
        max_position_embeddings=8192, bos_token_id=1, eos_token_id=2, pad_token_id=0,
        mlp_layer_types=["dense", "sparse"],
    )
    if real_attention:
        values.update(
            hidden_size=7168, num_attention_heads=64, num_key_value_heads=64,
            kv_lora_rank=512, q_lora_rank=1536, qk_rope_head_dim=64,
            qk_nope_head_dim=128, v_head_dim=128, index_topk=2048,
            index_head_dim=128, index_n_heads=64, gated_norm_rank=16,
        )
    cfg = AXK2Config(**values)
    cfg._attn_implementation = "eager"
    return cfg


def difference(actual, expected, atol, rtol, enforce=True):
    actual, expected = actual.float(), expected.float()
    delta = actual - expected
    metrics = {
        "max_absolute": delta.abs().max().item(),
        "relative_l2": (torch.linalg.vector_norm(delta) /
                        torch.linalg.vector_norm(expected).clamp_min(1e-12)).item(),
        "atol": atol, "rtol": rtol,
        "within_tolerance": torch.allclose(actual, expected, atol=atol, rtol=rtol),
    }
    if enforce:
        torch.testing.assert_close(actual, expected, atol=atol, rtol=rtol)
    return metrics


def cpu_cuda_reference():
    torch.manual_seed(42)
    cpu = AXK2ForCausalLM(config()).eval()
    gpu = copy.deepcopy(cpu).cuda()
    tokens = torch.randint(3, 128, (1, 12))
    with torch.inference_mode():
        reference = cpu(tokens, use_cache=False).logits
        observed = gpu(tokens.cuda(), use_cache=False).logits.cpu()
    result = difference(observed, reference, 1e-5, 1e-4)
    del gpu, cpu
    gc.collect()
    torch.cuda.empty_cache()
    return result


def attention_case(real_attention, dtype=torch.bfloat16, precision="baseline", seed=42,
                   backends=("eager", "sdpa"), sequence_length=None, decode_tokens=2,
                   realistic_ffn=False, original_experts=False):
    torch.manual_seed(seed)
    cfg = config(real_attention)
    if realistic_ffn:
        cfg.intermediate_size = 18432
        cfg.moe_intermediate_size = 2048
    if original_experts:
        cfg.n_routed_experts = 256
        cfg.num_experts_per_tok = 8
        cfg.n_group = 8
        cfg.topk_group = 4
        cfg.rope_parameters = dict(
            rope_type="yarn", rope_theta=1000000, factor=2.0,
            beta_fast=32.0, beta_slow=1.0, mscale=1.0,
            mscale_all_dim=1.0, original_max_position_embeddings=131072,
        )
        cfg.max_position_embeddings = 262144
    model = AXK2ForCausalLM(cfg).eval()
    # Match from_pretrained's AXK2 precision exceptions, and use identical initial
    # FP32 weights across dtype controls rather than sampling directly in BF16.
    fp32_index_weights = [
        copy.deepcopy(layer.self_attn.indexer.weights_proj) for layer in model.model.layers
    ]
    model = model.to(dtype=dtype).cuda()
    for layer, weights_proj in zip(model.model.layers, fp32_index_weights):
        layer.self_attn.indexer.weights_proj = weights_proj.cuda()
        layer_mlp = layer.mlp
        if hasattr(layer_mlp, "gate"):
            layer_mlp.gate.e_score_correction_bias = layer_mlp.gate.e_score_correction_bias.float()
    if precision == "fp32_attention":
        for layer in model.model.layers:
            attention = layer.self_attn.float()
            original_forward = attention.forward

            def make_forward(forward):
                def fp32_forward(self, hidden_states, position_embeddings, attention_mask, **kwargs):
                    input_dtype = hidden_states.dtype
                    mask = attention_mask if attention_mask.dtype == torch.bool else attention_mask.float()
                    output, weights = forward(
                        hidden_states=hidden_states.float(),
                        position_embeddings=tuple(t.float() for t in position_embeddings),
                        attention_mask=mask, **kwargs,
                    )
                    return output.to(input_dtype), weights
                return fp32_forward

            attention.forward = MethodType(make_forward(original_forward), attention)
    elif precision == "fp32_compute_bf16_ffn_storage":
        model.float()
        for layer in model.model.layers:
            if hasattr(layer.mlp, "experts"):
                layer.mlp.experts = FP32ExpertsWithBF16Storage(layer.mlp.experts)
                layer.mlp.shared_experts = FP32MLPWithBF16Storage(layer.mlp.shared_experts)
            else:
                layer.mlp = FP32MLPWithBF16Storage(layer.mlp)
    elif precision != "baseline":
        raise ValueError(f"Unknown precision mode: {precision}")
    sequence = sequence_length or (4096 if real_attention else 12)
    assert 0 < decode_tokens < sequence
    tokens = torch.randint(3, 128, (1, sequence), device="cuda")
    results = {
        "real_attention_dimensions": real_attention,
        "dtype": str(dtype),
        "precision_mode": precision, "seed": seed,
        "decode_tokens": decode_tokens, "realistic_ffn": realistic_ffn,
        "original_experts_and_rope": original_experts,
        "indexer_weights_proj_dtype": str(model.model.layers[0].self_attn.indexer.weights_proj.weight.dtype),
        "initialization": "Identical seeded FP32 initialization; AXK2 FP32 loading exceptions preserved.",
        "parameter_count": sum(p.numel() for p in model.parameters()),
        "sequence_length": sequence, "index_topk": cfg.index_topk,
        "config": cfg.to_dict(), "backends": {},
    }
    full_outputs, selections = {}, {}
    for backend in backends:
        model.set_attn_implementation(backend)
        assert model.config._attn_implementation == backend
        captured = {}
        phase = "full"

        def index_hook(layer_index):
            def record_index(module, inputs, output):
                captured.setdefault(phase, {})[layer_index] = output[:, -decode_tokens:].detach().cpu()
            return record_index

        hooks = [
            layer.self_attn.indexer.register_forward_hook(index_hook(i))
            for i, layer in enumerate(model.model.layers)
        ]
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            full = model(tokens, use_cache=False).logits
            torch.cuda.synchronize()
            prefill_seconds = time.perf_counter() - started
            assert torch.isfinite(full).all()
            full_outputs[backend] = full.detach().float().cpu()
            selections[backend] = captured["full"][0][:, -1]
            selected = selections[backend]
            assert selected.shape == (1, cfg.index_topk)
            assert selected.unique().numel() == cfg.index_topk
            assert int(selected.min()) >= 0 and int(selected.max()) < sequence

            phase = "prefix"
            prefix = model(tokens[:, :-decode_tokens], use_cache=True)
            cache = prefix.past_key_values
            del prefix
            decoded = []
            torch.cuda.synchronize()
            decode_started = time.perf_counter()
            for i in range(sequence - decode_tokens, sequence):
                phase = f"decode_{i}"
                step = model(tokens[:, i:i + 1], past_key_values=cache, use_cache=True)
                cache = step.past_key_values
                decoded.append(step.logits)
            torch.cuda.synchronize()
            decode_seconds = time.perf_counter() - decode_started
            tolerance = 0.02 if dtype == torch.bfloat16 and precision != "fp32_compute_bf16_ffn_storage" else 1e-4
            metrics = difference(
                torch.cat(decoded, dim=1), full[:, -decode_tokens:], tolerance, tolerance, enforce=False,
            )
            overlaps = {
                str(layer): [
                    torch.isin(
                        captured["full"][layer][0, offset],
                        captured[f"decode_{sequence - decode_tokens + offset}"][layer][0, 0],
                    ).float().mean().item()
                    for offset in range(decode_tokens)
                ]
                for layer in range(2)
            }
            results["backends"][backend] = {
                "prefill_seconds": prefill_seconds,
                "decode_seconds": decode_seconds,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
                "cached_decode_difference": metrics, "finite": True,
                "full_vs_cached_topk_overlap_per_layer": overlaps,
            }
            del full, cache, step, decoded
        for hook in hooks:
            hook.remove()
        torch.cuda.empty_cache()
    tolerance = 0.02 if dtype == torch.bfloat16 and precision != "fp32_compute_bf16_ffn_storage" else 1e-4
    results["passed"] = all(
        v["cached_decode_difference"]["within_tolerance"] for v in results["backends"].values()
    )
    if len(backends) == 2:
        results["eager_sdpa_difference"] = difference(
            full_outputs["sdpa"], full_outputs["eager"], tolerance, tolerance, enforce=False,
        )
        results["first_layer_last_query_topk_equal"] = torch.equal(
            selections["eager"].sort().values, selections["sdpa"].sort().values,
        )
        results["passed"] &= results["eager_sdpa_difference"]["within_tolerance"]
        assert results["first_layer_last_query_topk_equal"]
    del model
    gc.collect()
    torch.cuda.empty_cache()
    return results


if __name__ == "__main__":
    assert torch.cuda.is_available(), "CUDA is required; never silently substitute CPU."
    assert torch.cuda.get_device_capability() == (8, 0), "Expected A100 SM80."
    assert "A100" in torch.cuda.get_device_name(), "Expected an actual A100."
    torch.set_num_threads(4)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    result = {
        "scope": "Random reduced 2-layer AXK2 on one A100; not 688B weights, FP8 loading, production benchmarks or distributed inference.",
        "transformers_revision": REVISION, "transformers_version": transformers.__version__,
        "torch": torch.__version__, "cuda_build": torch.version.cuda, "python": platform.python_version(),
        "gpu": torch.cuda.get_device_name(), "compute_capability": list(torch.cuda.get_device_capability()),
        "total_gpu_bytes": torch.cuda.get_device_properties(0).total_memory,
        "nvidia_smi": subprocess.check_output(["nvidia-smi"], text=True), "passed": False,
    }
    write_result(result)
    try:
        result["tiny_cpu_cuda_fp32"] = cpu_cuda_reference()
        write_result(result)
        result["tiny_bf16"] = attention_case(False)
        write_result(result)
        result["original_attention_bf16"] = attention_case(True)
        write_result(result)
        result["original_attention_fp32"] = attention_case(True, torch.float32)
        result["passed"] = all(result[key]["passed"] for key in (
            "tiny_bf16", "original_attention_bf16", "original_attention_fp32",
        ))
        write_result(result)
        if not result["passed"]:
            raise AssertionError("One or more numerical comparisons failed; see recorded diagnostics.")
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        write_result(result)
        raise
