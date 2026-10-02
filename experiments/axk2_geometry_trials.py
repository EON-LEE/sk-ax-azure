"""One dense + one full-width 256-expert AXK2 layer on a single A100."""
import json
import traceback
from pathlib import Path
import torch
from axk2_cuda_smoke import attention_case

torch.set_num_threads(8)
torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.allow_tf32 = False
torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False
result = {
    "scope": "Original hidden/attention/FFN/256-expert/top8/grouping/YaRN dimensions. Only 2 layers and vocab128; random weights. No public model weights or distributed deployment.",
    "trials": {},
}
for seed in (42, 2026):
    name = f"seed{seed}_context4096_decode32"
    try:
        trial = attention_case(
            True, torch.bfloat16, "fp32_compute_bf16_ffn_storage", seed, backends=("eager",),
            sequence_length=4096, decode_tokens=32, realistic_ffn=True, original_experts=True,
        )
        result["trials"][name] = trial
    except Exception as exc:
        result["trials"][name] = {"error": str(exc), "traceback": traceback.format_exc()}
    Path("/tmp/axk2-geometry-results.json").write_text(json.dumps(result, indent=2))
    print(name, json.dumps(result["trials"][name]), flush=True)
