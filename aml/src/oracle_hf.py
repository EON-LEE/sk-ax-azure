"""Reference prompt log-probabilities for the 2-layer smoke checkpoint.

Runs the official Transformers AXK2 implementation (pinned commit) in FP32 on CPU with the official
sparse DSA attention (index_topk=2048). The vLLM deployment serves the same weights in dense mode,
so positions below 2048 must agree up to kernel numerics, while later positions show the size of
the dense-mode approximation on real weights. Experts stay in FP8 and are expanded per use, and
dequantization honours the partial 128x128 scale block of kv_a_proj_with_mqa (576 rows), exactly
as in the earlier real-weight A100 experiments.
"""
import argparse
import json
import os
import time
from pathlib import Path

import torch
import transformers
from safetensors import safe_open
from transformers import AXK2Config
from transformers.integrations.finegrained_fp8 import Fp8Dequantize
from transformers.models.axk2.modeling_axk2 import AXK2ForCausalLM, AXK2RotaryEmbedding

DEQUANT = Fp8Dequantize(None)


class Checkpoint:
    def __init__(self, root):
        self.root = Path(root)
        self.map = json.loads((self.root / "model.safetensors.index.json").read_text())["weight_map"]
        self.handles, self.used = {}, set()

    def get(self, name):
        shard = self.map[name]
        if shard not in self.handles:
            self.handles[shard] = safe_open(str(self.root / shard), framework="pt")
        self.used.add(name)
        return self.handles[shard].get_tensor(name)

    def expanded(self, name):
        tensor = self.get(name)
        if tensor.dtype == torch.float8_e4m3fn:
            return dequantize(tensor, self.get(name + "_scale_inv"))
        return tensor.float()


def dequantize(weight, scales):
    rows, cols = weight.shape
    assert scales.shape == ((rows + 127) // 128, (cols + 127) // 128), (weight.shape, scales.shape)
    if rows % 128 == 0 and cols % 128 == 0:
        return DEQUANT._dequantize_one(weight, scales, torch.float32)
    grid = scales.float().repeat_interleave(128, 0).repeat_interleave(128, 1)
    return weight.float() * grid[:rows, :cols]


class StoredFP8Experts(torch.nn.Module):
    """Original FP8 expert weights; FP32 projection expansion only for experts actually routed to."""

    def __init__(self, checkpoint, layer, count):
        super().__init__()
        self.count = count
        for expert in range(count):
            for short, projection in (("g", "gate_proj"), ("u", "up_proj"), ("d", "down_proj")):
                name = f"model.layers.{layer}.mlp.experts.{expert}.{projection}.weight"
                self.register_buffer(f"{short}{expert}", checkpoint.get(name))
                self.register_buffer(f"{short}{expert}s", checkpoint.get(name + "_scale_inv"))
        self.used = set()

    def projection(self, short, expert, inputs):
        weight = dequantize(getattr(self, f"{short}{expert}"), getattr(self, f"{short}{expert}s"))
        return torch.nn.functional.linear(inputs, weight)

    def forward(self, hidden_states, top_k_index, top_k_weights):
        output = torch.zeros_like(hidden_states)
        for expert in top_k_index.unique(sorted=True).tolist():
            self.used.add(expert)
            slot, token = torch.where((top_k_index == expert).transpose(0, 1))
            inputs = hidden_states[token]
            hidden = torch.nn.functional.silu(self.projection("g", expert, inputs)) * self.projection("u", expert, inputs)
            output.index_add_(0, token, self.projection("d", expert, hidden) * top_k_weights[token, slot, None])
        return output


def build(checkpoint):
    raw = json.loads((checkpoint.root / "config.json").read_text())
    raw.pop("quantization_config", None)
    config = AXK2Config.from_dict(raw)
    config._attn_implementation = "eager"
    with torch.device("meta"):
        model = AXK2ForCausalLM(config)
    for index, layer in enumerate(model.model.layers):
        if hasattr(layer.mlp, "experts"):
            layer.mlp.experts = StoredFP8Experts(checkpoint, index, config.n_routed_experts)
    for name, parameter in list(model.named_parameters()):
        source = (name.replace(".mlp.fc1.", ".W_down.").replace(".mlp.fc2.", ".W_up.")
                  .replace(".self_attn.q_gate_proj.", ".self_attn.q_b_proj."))
        value = checkpoint.expanded(source)
        assert tuple(value.shape) == tuple(parameter.shape), (name, value.shape, parameter.shape)
        parent, _, leaf = name.rpartition(".")
        model.get_submodule(parent).register_parameter(leaf, torch.nn.Parameter(value, requires_grad=False))
    model.model.rotary_emb = AXK2RotaryEmbedding(config=config)
    for name, buffer in list(model.named_buffers()):
        if buffer.is_meta:
            value = checkpoint.expanded(name)
            assert tuple(value.shape) == tuple(buffer.shape), (name, value.shape, buffer.shape)
            parent, _, leaf = name.rpartition(".")
            model.get_submodule(parent).register_buffer(leaf, value)
    unused = sorted(set(checkpoint.map) - checkpoint.used)
    assert not unused, unused[:10]
    assert not any(t.is_meta for t in (*model.parameters(), *model.buffers()))
    return model.eval(), config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    torch.set_num_threads(os.cpu_count() or 8)
    checkpoint = Checkpoint(args.ckpt)
    began = time.time()
    model, config = build(checkpoint)
    load_seconds = time.time() - began
    prompts = json.loads((checkpoint.root / "smoke_prompts.json").read_text())
    series, summary = {}, {}
    with torch.inference_mode():
        for prompt in prompts:
            started = time.time()
            ids = torch.tensor([prompt["token_ids"]])
            logits = model(input_ids=ids, use_cache=False, logits_to_keep=0).logits[0].float()
            logp = torch.log_softmax(logits, dim=-1)[:-1]
            chosen = logp.gather(1, ids[0, 1:, None])[:, 0]
            series[f"{prompt['name']}.chosen_logprob"] = [round(v, 5) for v in chosen.tolist()]
            series[f"{prompt['name']}.top1"] = logp.argmax(-1).tolist()
            summary[prompt["name"]] = {"tokens": len(prompt["token_ids"]), "seconds": round(time.time() - started, 1),
                                       "finite": bool(torch.isfinite(logits).all())}
            del logits, logp
    result = {"scope": "Official Transformers AXK2, FP32 CPU, sparse DSA (index_topk=2048), real weights of layers 0-1.",
              "transformers": transformers.__version__, "torch": torch.__version__,
              "index_topk": config.index_topk, "load_seconds": round(load_seconds, 1), "prompts": summary}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2))
    Path(args.out).with_name("oracle-series.json").write_text(json.dumps(series))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
