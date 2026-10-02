"""Locate first backend divergence and discrete routing changes."""
import json
from pathlib import Path

import torch
from transformers import AXK2ForCausalLM
from axk2_cuda_smoke import config, difference

torch.set_num_threads(4)
torch.manual_seed(42)
torch.backends.cuda.matmul.allow_tf32 = False
torch.backends.cudnn.allow_tf32 = False
torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)
torch.backends.cuda.enable_cudnn_sdp(False)
torch.backends.cuda.enable_math_sdp(True)
model = AXK2ForCausalLM(config(True)).eval().cuda()
tokens = torch.randint(3, 128, (1, 4096), device="cuda")
captured = {}
backend = "eager"


def capture(name):
    def hook(module, args, output):
        tensor = output[0] if isinstance(output, tuple) else output
        captured[backend][name] = tensor.detach().cpu()
        if name == "router":
            captured[backend]["router_ids"] = output[2].detach().cpu()
    return hook


hooks = []
for i, layer in enumerate(model.model.layers):
    for name, module in (
        ("index", layer.self_attn.indexer), ("attn", layer.self_attn),
        ("input_norm", layer.input_layernorm), ("mlp", layer.mlp), ("layer", layer),
    ):
        hooks.append(module.register_forward_hook(capture(f"{i}_{name}")))
hooks.append(model.model.layers[1].mlp.gate.register_forward_hook(capture("router")))
for backend in ("eager", "sdpa"):
    model.set_attn_implementation(backend)
    captured[backend] = {}
    with torch.inference_mode():
        captured[backend]["logits"] = model(tokens, use_cache=False).logits.cpu()
for hook in hooks:
    hook.remove()
result = {"scope": "Random reduced model, FP32, math SDPA vs eager; no source model modifications.", "comparisons": {}}
for name, ref in captured["eager"].items():
    actual = captured["sdpa"][name]
    if "index" in name or name == "router_ids":
        changed = (ref.sort(dim=-1).values != actual.sort(dim=-1).values).any(dim=-1)
        result["comparisons"][name] = {
            "changed_rows": int(changed.sum()),
            "changed_positions": changed.nonzero().tolist()[:30],
        }
    else:
        diff = difference(actual, ref, 1e-4, 1e-4, enforce=False)
        delta = (actual - ref).abs()
        location = torch.unravel_index(delta.argmax(), delta.shape)
        diff["largest_position"] = [int(i) for i in location]
        diff["reference_at_largest"] = ref[location].item()
        diff["observed_at_largest"] = actual[location].item()
        result["comparisons"][name] = diff
Path("/tmp/axk2-trace-results.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
