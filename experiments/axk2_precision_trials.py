"""Investigate numerical remedies without widening the original tolerances."""
import json
import traceback
from pathlib import Path

import torch
from axk2_cuda_smoke import attention_case

torch.set_num_threads(4)
torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.allow_tf32 = False
torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False
assert torch.cuda.get_device_capability() == (8, 0)
result = {
    "scope": "Single A100, random 2-layer AXK2, real attention dimensions, reduced experts. Experimental precision changes, not supported production configuration.",
    "trials": {},
}
for name, dtype, precision, math_only in (
    ("bf16_no_reduced_accumulation", torch.bfloat16, "baseline", False),
    ("bf16_math_sdpa", torch.bfloat16, "baseline", True),
    ("bf16_fp32_attention_math", torch.bfloat16, "fp32_attention", True),
    ("fp32_math_sdpa", torch.float32, "baseline", True),
):
    torch.backends.cuda.enable_flash_sdp(not math_only)
    torch.backends.cuda.enable_mem_efficient_sdp(not math_only)
    torch.backends.cuda.enable_cudnn_sdp(not math_only)
    torch.backends.cuda.enable_math_sdp(True)
    try:
        trial = attention_case(True, dtype, precision)
        trial.pop("config")
        result["trials"][name] = trial
        print(name, json.dumps(trial), flush=True)
    except Exception as exc:
        result["trials"][name] = {"error": str(exc), "traceback": traceback.format_exc()}
        print(name, traceback.format_exc(), flush=True)
    Path("/tmp/axk2-precision-results.json").write_text(json.dumps(result, indent=2))
