"""Preserve a failed chunked-prefill control; do not silently relax its tolerance."""
import json
from pathlib import Path

import torch
from transformers import AXK2ForCausalLM

from axk2_request_stage import AXK2RequestStage
from validate_axk2_request_pipeline import config

torch.set_num_threads(1)
torch.manual_seed(42)
model = AXK2ForCausalLM(config()).eval()
ids = torch.randint(3, 128, (1, 19))
stage = AXK2RequestStage(
    model.config, list(model.model.layers), model.model.rotary_emb, 0, 1, 0,
    embedding=model.model.embed_tokens, norm=model.model.norm, head=model.lm_head,
).eval()
errors = []
with torch.no_grad():
    full = model(ids, use_cache=True).logits[:, -1]
    cache = None
    for start in range(0, 19, 6):
        chunk = ids[:, start:start + 6]
        reference = model(chunk, past_key_values=cache, use_cache=True)
        cache = reference.past_key_values
        actual = stage(chunk, torch.tensor([[0, start, chunk.shape[1]]]))[0]
        errors.append((actual - reference.logits[:, -1]).abs().max().item())
        torch.testing.assert_close(actual, reference.logits[:, -1], atol=1e-4, rtol=1e-4)
    passed = torch.allclose(full, reference.logits[:, -1], atol=1e-4, rtol=1e-4)
result = {
    "scope": "Small random61-layer AXK2 on CPU; not released weights, not GPUs.",
    "transformers_revision": "7fb5bcd1d4b8a5c225a2c33429b2e9e023dd61ae",
    "seed": 42, "tokens": ids.tolist(), "chunk": 6,
    "stage_vs_official_matching_chunks_max_errors": errors,
    "official_full_vs_official_chunked_max_error": (full - reference.logits[:, -1]).abs().max().item(),
    "official_full_vs_chunked_passed_at_1e4": bool(passed),
    "decision": "Disable chunked prefill in full-model initial trial until separately justified with actual weights.",
    "cause": "Not established. The discrepancy reproduces inside the unmodified official model, without distributed execution.",
}
(Path(__file__).resolve().parent / "axk2-chunking-diagnostic.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
