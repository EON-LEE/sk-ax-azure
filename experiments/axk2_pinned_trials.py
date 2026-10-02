"""Validate fixed-backend cache consistency across seeds; not cross-backend parity."""
import json
import traceback
from pathlib import Path
import torch
from axk2_cuda_smoke import attention_case

torch.set_num_threads(4)
torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.allow_tf32 = False
torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False
result = {"scope": "Pinned eager backend only; numerical cache consistency, not eager/SDPA equivalence.", "trials": {}}
for seed in (42, 123, 2026):
    for precision, dtype in (("fp32_attention", torch.bfloat16), ("baseline", torch.float32)):
        name = f"{precision}_{dtype}_seed{seed}"
        try:
            trial = attention_case(True, dtype, precision, seed, backends=("eager",))
            trial.pop("config")
            result["trials"][name] = trial
        except Exception as exc:
            result["trials"][name] = {"error": str(exc), "traceback": traceback.format_exc()}
        Path("/tmp/axk2-pinned-results.json").write_text(json.dumps(result, indent=2))
        print(name, json.dumps(result["trials"][name]), flush=True)
