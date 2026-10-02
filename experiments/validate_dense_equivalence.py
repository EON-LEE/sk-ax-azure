"""Miniature CPU check of the A.X-K2 dense-mode premise, with random weights.

The pinned SKT vLLM fork serves the DSA checkpoint as plain causal MLA when the
config has no ``index_topk``. DeepSeek Sparse Attention keeps the top
``index_topk`` visible keys per query, so it should equal dense causal attention
while a query sees at most ``index_topk`` tokens, and differ afterwards.

This checks that property with the pinned Transformers AXK2 code at miniature
scale. It does not prove the vLLM kernels, the 688B weights, or quality beyond
``index_topk`` tokens.
"""

import copy
import json
from pathlib import Path

TRANSFORMERS_SHA = "7fb5bcd1d4b8a5c225a2c33429b2e9e023dd61ae"
ROOT = Path(__file__).resolve().parent
TOPK = 8
LENGTH = 3 * TOPK
PROMPT = 3
SAME = dict(rtol=1e-5, atol=1e-6)


def select_every_key(indexer):
    """Turn one DSA layer into dense causal MLA, keeping the indexer-cache bookkeeping."""
    import torch

    original = indexer.forward

    def forward(hidden_states, q_resid, position_embeddings, attention_mask, position_ids, past_key_values=None):
        original(hidden_states, q_resid, position_embeddings, attention_mask, position_ids,
                 past_key_values=past_key_values)
        batch, queries = hidden_states.shape[:2]
        keys = attention_mask.shape[-1]
        return torch.arange(keys, dtype=torch.int32).expand(batch, queries, keys).contiguous()

    indexer.forward = forward


def build_models(attention):
    import torch
    from transformers import AXK2Config, AXK2ForCausalLM

    torch.manual_seed(7)
    config = AXK2Config(
        vocab_size=128, hidden_size=32, intermediate_size=64, moe_intermediate_size=16,
        num_hidden_layers=3, num_attention_heads=4, num_key_value_heads=4,
        n_shared_experts=1, n_routed_experts=4, n_group=2, topk_group=1,
        num_experts_per_tok=2, kv_lora_rank=16, q_lora_rank=16,
        qk_rope_head_dim=8, qk_nope_head_dim=8, v_head_dim=8,
        index_topk=TOPK, index_head_dim=16, index_n_heads=4, gated_norm_rank=4,
        max_position_embeddings=128, bos_token_id=1, eos_token_id=2, pad_token_id=0,
        mlp_layer_types=["dense", "sparse", "sparse"],
    )
    config._attn_implementation = attention
    sparse = AXK2ForCausalLM(config).eval()
    dense = copy.deepcopy(sparse)
    for layer in dense.model.layers:
        select_every_key(layer.self_attn.indexer)
    return sparse, dense


def per_position(a, b):
    return (a - b).abs().amax(dim=-1)[0].tolist()


def cached_logits(model, tokens):
    import torch

    output = model(tokens[:, :PROMPT], use_cache=True)
    cache, logits = output.past_key_values, [output.logits]
    for i in range(PROMPT, tokens.shape[1]):
        output = model(tokens[:, i:i + 1], past_key_values=cache, use_cache=True)
        cache = output.past_key_values
        logits.append(output.logits)
    return torch.cat(logits, dim=1)


def greedy(model, prompt, length):
    output = model(prompt, use_cache=True)
    generated = prompt[0].tolist()
    while len(generated) < length:
        token = output.logits[:, -1:].argmax(-1)
        generated.append(int(token))
        output = model(token, past_key_values=output.past_key_values, use_cache=True)
    return generated


def run(attention):
    import torch

    sparse, dense = build_models(attention)
    tokens = torch.randint(3, 128, (1, LENGTH), generator=torch.Generator().manual_seed(11))
    selections = []
    hook = sparse.model.layers[0].self_attn.indexer.register_forward_hook(
        lambda module, inputs, output: selections.append(output.detach().clone())
    )
    with torch.inference_mode():
        sparse_full = sparse(tokens, use_cache=False).logits
        hook.remove()
        dense_full = dense(tokens, use_cache=False).logits
        sparse_cached = cached_logits(sparse, tokens)
        dense_cached = cached_logits(dense, tokens)
        sparse_tokens = greedy(sparse, tokens[:, :PROMPT], LENGTH)
        dense_tokens = greedy(dense, tokens[:, :PROMPT], LENGTH)

    visible = [int((selections[0][0, s] <= s).sum()) for s in range(LENGTH)]
    prefill, cached = per_position(sparse_full, dense_full), per_position(sparse_cached, dense_cached)
    # Position s attends to s + 1 tokens, so it is inside the DSA budget while s < TOPK.
    assert visible == [min(s + 1, TOPK) for s in range(LENGTH)], visible
    torch.testing.assert_close(sparse_full[:, :TOPK], dense_full[:, :TOPK], **SAME)
    torch.testing.assert_close(sparse_cached[:, :TOPK], dense_cached[:, :TOPK], **SAME)
    torch.testing.assert_close(sparse_cached[:, :TOPK], sparse_full[:, :TOPK], rtol=1e-4, atol=1e-5)
    torch.testing.assert_close(dense_cached, dense_full, rtol=1e-4, atol=1e-5)
    # Not asserted: beyond TOPK, ~1e-9 cache-path noise can flip near-tied hard top-k choices.
    sparse_path = per_position(sparse_cached, sparse_full)
    # Non-vacuous: once tokens are dropped, the two attentions must really differ.
    assert min(prefill[TOPK:]) > 100 * max(prefill[:TOPK] + [1e-7]), prefill
    # Token t is predicted from t visible tokens, so tokens 0..TOPK must agree.
    assert sparse_tokens[:TOPK + 1] == dense_tokens[:TOPK + 1]
    diverged = next((i for i, (a, b) in enumerate(zip(sparse_tokens, dense_tokens)) if a != b), None)
    return {
        "attention_implementation": attention,
        "layer0_visible_keys_selected_by_query_position": visible,
        "prefill_max_abs_error_by_position": prefill,
        "prefill_max_abs_error_within_topk": max(prefill[:TOPK]),
        "prefill_min_abs_error_beyond_topk": min(prefill[TOPK:]),
        "cached_decode_max_abs_error_by_position": cached,
        "cached_decode_max_abs_error_within_topk": max(cached[:TOPK]),
        "dense_cached_vs_full_max_abs_error": (dense_cached - dense_full).abs().max().item(),
        "sparse_cached_vs_full_max_abs_error_by_position": sparse_path,
        "sparse_cached_vs_full_note": (
            "Observation, not asserted: beyond index_topk the sparse model can choose different near-tied "
            "keys on the cached path because hard top-k is discontinuous; dense attention has no selection."
        ),
        "greedy_tokens_sparse": sparse_tokens,
        "greedy_tokens_dense": dense_tokens,
        "greedy_tokens_required_equal_through_index": TOPK,
        "greedy_first_divergence_index": diverged,
        "passed": True,
    }


def main():
    import torch
    import transformers

    torch.set_num_threads(2)
    report = {
        "scope": "CPU only, random miniature 3-layer AXK2; NOT released weights, vLLM kernels or GPU proof.",
        "claim_tested": (
            "DSA top-k attention equals dense causal MLA for every query position below index_topk "
            "and differs beyond it, in prefill, cached decode and greedy generation."
        ),
        "real_model_implication": (
            "A.X-K2 has index_topk=2048, so dense mode should match sparse mode for every token whose "
            "causal context is at most 2048 tokens, up to kernel numerics; longer contexts need quality evaluation."
        ),
        "transformers_revision": TRANSFORMERS_SHA,
        "transformers_version": transformers.__version__,
        "torch_version": torch.__version__,
        "index_topk": TOPK, "sequence_length": LENGTH, "prompt_length": PROMPT,
        "results": {attention: run(attention) for attention in ("eager", "sdpa")},
        "passed": True,
    }
    output = ROOT / "axk2-dense-equivalence-results.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    for attention, result in report["results"].items():
        print(attention, {key: value for key, value in result.items() if not isinstance(value, list)})
    print(f"Saved results: {output}")


if __name__ == "__main__":
    main()
