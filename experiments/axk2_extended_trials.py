"""Stress the candidate with original FFN widths and more cached tokens."""
import json
import traceback
from pathlib import Path
import torch
from axk2_cuda_smoke import attention_case

torch.set_num_threads(4)
torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.allow_tf32 = False
torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = False
result = {
    "scope": "Original attention and FFN widths, 2 layers, 4 routed experts/top2 instead of 256/top8. Fixed eager. Random weights; NOT 688B or quality evaluation.",
    "trials": {},
}
for seed, sequence, decode in ((42, 4096, 32), (123, 4096, 32), (2026, 4096, 32), (42, 8192, 8)):
    name = f"seed{seed}_context{sequence}_decode{decode}"
    try:
        trial = attention_case(
            True, torch.bfloat16, "fp32_attention", seed, backends=("eager",),
            sequence_length=sequence, decode_tokens=decode, realistic_ffn=True,
        )
        trial.pop("config")
        result["trials"][name] = trial
    except Exception as exc:
        result["trials"][name] = {"error": str(exc), "traceback": traceback.format_exc()}
    Path("/tmp/axk2-extended-results.json").write_text(json.dumps(result, indent=2))
    print(name, json.dumps(result["trials"][name]), flush=True)
