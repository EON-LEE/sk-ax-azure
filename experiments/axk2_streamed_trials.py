"""Test BF16 FFN storage with FP32 calculations and per-expert expansion."""
import copy
import json
import traceback
from pathlib import Path
import torch
from transformers import AXK2ForCausalLM
from axk2_cuda_smoke import (
    config, difference, attention_case, FP32ExpertsWithBF16Storage, FP32MLPWithBF16Storage,
)

torch.set_num_threads(4)
torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.allow_tf32 = False
torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False
torch.manual_seed(42)
result = {
    "scope": "Experimental BF16 FFN weight storage, FP32 calculations, fixed eager; one active expert expanded at a time. Not optimized serving.",
    "operator_checks": {}, "trials": {},
}
model = AXK2ForCausalLM(config()).cuda().eval()
x = torch.randn((12, 32), device="cuda")
ids = torch.tensor([[0, 1], [1, 2], [2, 3]] * 4, device="cuda")
weights = torch.softmax(torch.randn(12, 2, device="cuda"), -1)
with torch.inference_mode():
    reference_experts = copy.deepcopy(model.model.layers[1].mlp.experts).bfloat16().float()
    streamed = FP32ExpertsWithBF16Storage(copy.deepcopy(reference_experts))
    result["operator_checks"]["experts"] = difference(
        streamed(x, ids, weights), reference_experts(x, ids, weights), 1e-6, 1e-5,
    )
    reference_mlp = copy.deepcopy(model.model.layers[0].mlp).bfloat16().float()
    mixed_mlp = FP32MLPWithBF16Storage(copy.deepcopy(reference_mlp))
    result["operator_checks"]["mlp"] = difference(mixed_mlp(x), reference_mlp(x), 1e-6, 1e-5)
del model, reference_experts, streamed, reference_mlp, mixed_mlp
torch.cuda.empty_cache()
for seed, sequence, decode in ((42, 4096, 32), (123, 4096, 32), (2026, 4096, 32), (42, 8192, 8)):
    name = f"seed{seed}_context{sequence}_decode{decode}"
    try:
        trial = attention_case(
            True, torch.bfloat16, "fp32_compute_bf16_ffn_storage", seed, backends=("eager",),
            sequence_length=sequence, decode_tokens=decode, realistic_ffn=True,
        )
        trial.pop("config")
        result["trials"][name] = trial
    except Exception as exc:
        result["trials"][name] = {"error": str(exc), "traceback": traceback.format_exc()}
    Path("/tmp/axk2-streamed-results.json").write_text(json.dumps(result, indent=2))
    print(name, json.dumps(result["trials"][name]), flush=True)
