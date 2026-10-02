"""Pinned source predicates and a random, miniature AXK2 CPU test, not 688B serving."""

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

VLLM_SHA = "023bc76025459d6356c5885f271b9b95c50f93b6"
TRANSFORMERS_SHA = "7fb5bcd1d4b8a5c225a2c33429b2e9e023dd61ae"
ROOT = Path(__file__).resolve().parent
SOURCES = {
    "base": "vllm/v1/attention/backend.py",
    "flashmla": "vllm/v1/attention/backends/mla/flashmla_sparse.py",
    "flashinfer": "vllm/v1/attention/backends/mla/flashinfer_mla_sparse.py",
    "triton": "vllm/v1/attention/backends/mla/triton_mla.py",
    "mla_common": "vllm/model_executor/layers/attention/mla_attention.py",
    "flashmla_build": "cmake/external_projects/flashmla.cmake",
    "deepgemm_build": "cmake/external_projects/deepgemm.cmake",
    "indexer": "vllm/model_executor/layers/sparse_attn_indexer.py",
    "platform": "vllm/platforms/cuda.py",
    "deepgemm": "vllm/utils/deep_gemm.py",
    "marlin_linear": "vllm/model_executor/kernels/linear/scaled_mm/marlin.py",
    "marlin_moe": "vllm/model_executor/layers/fused_moe/experts/marlin_moe.py",
    "model": "vllm/model_executor/models/axk2.py",
    "requirements": "requirements/cuda.txt",
}


def class_node(source, name):
    return next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == name)


def extracted_method(source, class_name, method):
    node = copy.deepcopy(
        next(n for n in class_node(source, class_name).body if isinstance(n, ast.FunctionDef) and n.name == method)
    )
    node.decorator_list = []
    node.returns = None
    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        arg.annotation = None
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "<extracted-method>", "exec"), namespace)
    return namespace[method]


def source_checks():
    texts, manifest = {}, {}
    cache = ROOT / "axk2-source-snapshots"
    cache.mkdir(exist_ok=True)
    for key, path in SOURCES.items():
        url = f"https://raw.githubusercontent.com/SKT-AI/vllm/{VLLM_SHA}/{path}"
        with urlopen(url, timeout=60) as response:
            data = response.read()
        (cache / (key + Path(path).suffix)).write_bytes(data)
        texts[key] = data.decode()
        manifest[key] = {"url": url, "sha256": hashlib.sha256(data).hexdigest()}

    gates = {
        key: extracted_method(texts[key], name, "supports_compute_capability")
        for key, name in (("flashmla", "FlashMLASparseBackend"), ("flashinfer", "FlashInferMLASparseBackend"))
    }
    matrix = {
        f"sm{major}{minor}": {key: gate(None, SimpleNamespace(major=major, minor=minor)) for key, gate in gates.items()}
        for major, minor in ((8, 0), (8, 6), (9, 0), (10, 0), (12, 0))
    }
    assert matrix["sm80"] == {"flashmla": False, "flashinfer": False}
    assert matrix["sm90"] == {"flashmla": True, "flashinfer": False}
    assert matrix["sm100"] == {"flashmla": True, "flashinfer": True}

    for key, name in (("triton", "TritonMLABackend"), ("mla_common", "MLACommonBackend")):
        assert not any(isinstance(n, ast.FunctionDef) and n.name == "is_sparse" for n in class_node(texts[key], name).body)
    sparse = extracted_method(texts["base"], "AttentionBackend", "is_sparse")
    validate = extracted_method(texts["base"], "AttentionBackend", "validate_configuration")
    # Other constraints deliberately pass: this isolates the real sparse-mismatch branch.
    stub = SimpleNamespace(
        supports_head_size=lambda _: True, supports_dtype=lambda _: True,
        supports_kv_cache_dtype=lambda _: True, supports_block_size=lambda _: True,
        is_mla=lambda: True, is_sparse=lambda: sparse(None),
        supports_compute_capability=lambda _: True, supports_attn_type=lambda _: True,
        supports_combination=lambda *args: None,
    )
    arguments = dict(
        head_size=576, dtype="bfloat16", kv_cache_dtype="auto", block_size=64,
        use_mla=True, has_sink=False, use_mm_prefix=False, use_per_head_quant_scales=False,
        device_capability=SimpleNamespace(major=8, minor=0), attn_type="decoder",
    )
    rejection = validate(stub, use_sparse=True, **arguments)
    assert rejection == ["sparse not supported"]
    assert validate(stub, use_sparse=False, **arguments) == []
    evidence = {}
    needles = (
        "supports_compute_capability", "capability.major", "sparse not supported",
        "GIT_TAG", "9.0a", "10.0a", "10.0f", "has_deep_gemm",
        "fp8_fp4_mqa_logits", "fp8_fp4_paged_mqa_logits", "support_deep_gemm",
        "has_device_capability(90)", "is_device_capability_family(100)",
        "kFp8Static128BlockSym", "SupportsPP", "flashinfer-python", "flashinfer-cubin",
    )
    for key, text in texts.items():
        evidence[key] = [
            {"line": i, "text": line.strip()}
            for i, line in enumerate(text.splitlines(), 1) if any(n in line for n in needles)
        ]
    return {
        "scope": "Extracted original Python predicates with mocked device metadata; no vLLM import or CUDA execution.",
        "vllm_revision": VLLM_SHA, "hardware_gate_matrix": matrix,
        "dense_triton_sparse_request": rejection, "sources": manifest, "evidence": evidence,
    }


def cpu_test():
    import torch
    import transformers
    from transformers import AXK2Config, AXK2ForCausalLM

    torch.set_num_threads(2)
    torch.manual_seed(42)
    config = AXK2Config(
        vocab_size=128, hidden_size=32, intermediate_size=64, moe_intermediate_size=16,
        num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=4,
        n_shared_experts=1, n_routed_experts=4, n_group=2, topk_group=1,
        num_experts_per_tok=2, kv_lora_rank=16, q_lora_rank=16,
        qk_rope_head_dim=8, qk_nope_head_dim=8, v_head_dim=8,
        index_topk=4, index_head_dim=16, index_n_heads=4, gated_norm_rank=4,
        max_position_embeddings=64, bos_token_id=1, eos_token_id=2, pad_token_id=0,
        mlp_layer_types=["dense", "sparse"],
    )
    config._attn_implementation = "eager"
    eager = AXK2ForCausalLM(config).eval()
    sdpa_config = copy.deepcopy(config)
    sdpa_config._attn_implementation = "sdpa"
    sdpa = AXK2ForCausalLM(sdpa_config).eval()
    sdpa.load_state_dict(eager.state_dict())
    assert eager.config._attn_implementation == "eager"
    assert sdpa.config._attn_implementation == "sdpa"
    tokens = torch.randint(3, 128, (1, 12))
    selections = []
    hook = eager.model.layers[0].self_attn.indexer.register_forward_hook(
        lambda module, inputs, output: selections.append(output.detach())
    )
    with torch.inference_mode():
        full_eager = eager(tokens, use_cache=False).logits
        full_sdpa = sdpa(tokens, use_cache=False).logits
        hook.remove()
        torch.testing.assert_close(full_eager, full_sdpa, rtol=1e-4, atol=1e-5)
        assert selections[0].shape == (1, 12, 4)
        assert (selections[0][0, -1] <= 11).all()
        cached_errors = {}
        for name, model, full in (("eager", eager, full_eager), ("sdpa", sdpa, full_sdpa)):
            prefix = model(tokens[:, :8], use_cache=True)
            cache = prefix.past_key_values
            decoded = []
            for i in range(8, 12):
                step = model(tokens[:, i:i + 1], past_key_values=cache, use_cache=True)
                cache = step.past_key_values
                decoded.append(step.logits)
            decoded = torch.cat(decoded, dim=1)
            torch.testing.assert_close(decoded, full[:, 8:], rtol=1e-4, atol=1e-5)
            cached_errors[name] = (decoded - full[:, 8:]).abs().max().item()
        assert torch.isfinite(full_eager).all()
        bf16 = copy.deepcopy(eager).to(torch.bfloat16)
        bf16_logits = bf16(tokens, use_cache=False).logits
        assert torch.isfinite(bf16_logits).all()
    return {
        "scope": "Random miniature 2-layer CPU AXK2, dense + grouped MoE + sparse indexer; NOT released weights or GPU proof.",
        "transformers_revision": TRANSFORMERS_SHA, "transformers_version": transformers.__version__,
        "torch_version": torch.__version__, "torch_cuda_build": torch.version.cuda,
        "parameter_count": sum(p.numel() for p in eager.parameters()),
        "device": str(next(eager.parameters()).device), "logits_shape": list(full_eager.shape),
        "attention_implementations": [eager.config._attn_implementation, sdpa.config._attn_implementation],
        "sequence_length": 12, "index_topk": 4, "indexer_output_shape": list(selections[0].shape),
        "eager_sdpa_max_absolute_error": (full_eager - full_sdpa).abs().max().item(),
        "cached_decode_max_absolute_error": cached_errors,
        "bf16_finite": True, "passed": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    result = {"source": source_checks()}
    (ROOT / "axk2-source-results.json").write_text(json.dumps(result, indent=2) + "\n")
    if not args.source_only:
        result["cpu"] = cpu_test()
    output = ROOT / "axk2-validation-results.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "source"}, indent=2))
    print(f"Saved results: {output}")
