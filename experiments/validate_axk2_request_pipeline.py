"""Native pipeline + real AXK2 architecture on CPU, with small random weights."""
import json
import socket
from datetime import timedelta
from pathlib import Path

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.distributed.pipelining import PipelineStage, ScheduleGPipe
from transformers import AXK2Config, AXK2ForCausalLM

from axk2_request_stage import AXK2RequestStage
from validate_native_pipeline import overlap

ROOT = Path(__file__).resolve().parent
STAGES, LAYERS, REQUESTS, CHUNK, GENERATED = 4, 61, 8, 20, 4


def config():
    value = AXK2Config(
        vocab_size=128, hidden_size=32, intermediate_size=64, moe_intermediate_size=16,
        num_hidden_layers=LAYERS, num_attention_heads=4, num_key_value_heads=4,
        n_shared_experts=1, n_routed_experts=4, n_group=2, topk_group=1,
        num_experts_per_tok=2, kv_lora_rank=16, q_lora_rank=16,
        qk_rope_head_dim=8, qk_nope_head_dim=8, v_head_dim=8,
        index_topk=4, index_head_dim=16, index_n_heads=4, gated_norm_rank=4,
        max_position_embeddings=128, bos_token_id=1, eos_token_id=2, pad_token_id=0,
        mlp_layer_types=["dense"] + ["sparse"] * (LAYERS - 1),
    )
    value._attn_implementation = "eager"
    return value


def worker(rank, port):
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.manual_seed(42)
    dist.init_process_group("gloo", rank=rank, world_size=STAGES,
                            init_method=f"tcp://127.0.0.1:{port}", timeout=timedelta(minutes=5))
    cfg = config()
    model = AXK2ForCausalLM(cfg).eval()
    first, last = LAYERS * rank // STAGES, LAYERS * (rank + 1) // STAGES
    module = AXK2RequestStage(
        cfg, list(model.model.layers)[first:last], model.model.rotary_emb,
        rank, STAGES, first,
        embedding=model.model.embed_tokens if rank == 0 else None,
        norm=model.model.norm if rank == STAGES - 1 else None,
        head=model.lm_head if rank == STAGES - 1 else None,
    ).eval()
    lengths = [5, 9, 13, 17, 7, 11, 15, 19]
    generator = torch.Generator().manual_seed(777)
    prompts = [torch.randint(3, cfg.vocab_size, (length,), generator=generator) for length in lengths]
    references, reference_logits = [], []
    if rank == STAGES - 1:
        with torch.no_grad():
            for prompt in prompts:
                result = model(prompt.unsqueeze(0), use_cache=True)
                reference_logits.append(result.logits[:, -1])
                ids = []
                for step in range(GENERATED):
                    token = result.logits[:, -1].argmax(-1)
                    ids.append(token.item())
                    if step < GENERATED - 1:
                        result = model(token.view(1, 1), past_key_values=result.past_key_values, use_cache=True)
                references.append(ids)
    dist.barrier()

    def schedule(tokens):
        payload = (torch.zeros(1, tokens, dtype=torch.long) if rank == 0
                   else torch.zeros(1, tokens, cfg.hidden_size))
        metadata = torch.zeros(1, 3, dtype=torch.long)
        output = torch.zeros(1, cfg.vocab_size) if rank == STAGES - 1 else torch.zeros(1, tokens, cfg.hidden_size)
        native_stage = PipelineStage(module, rank, STAGES, torch.device("cpu"),
                                     input_args=(payload, metadata), output_args=(output, metadata.clone()))
        return ScheduleGPipe(native_stage, n_microbatches=REQUESTS, loss_fn=None)

    prefill = schedule(CHUNK)
    with torch.no_grad():
        for offset in range(0, max(lengths), CHUNK):
            ids = torch.zeros(REQUESTS, CHUNK, dtype=torch.long)
            metadata = torch.zeros(REQUESTS, 3, dtype=torch.long)
            for slot, prompt in enumerate(prompts):
                start = min(offset, len(prompt))
                valid = min(CHUNK, len(prompt) - start)
                ids[slot, :valid] = prompt[start:start + valid]
                metadata[slot] = torch.tensor([slot, start, valid])
            result = prefill.step(ids, metadata) if rank == 0 else prefill.step()
    assert module.positions == dict(enumerate(lengths))
    for slot, length in enumerate(lengths):
        assert module.caches[slot].get_seq_length(first) == length
    prefill_error = None
    tokens = torch.zeros(REQUESTS, dtype=torch.long)
    if rank == STAGES - 1:
        expected = torch.cat(reference_logits)
        torch.testing.assert_close(result[0], expected, atol=1e-4, rtol=1e-4)
        prefill_error = (result[0] - expected).abs().max().item()
        tokens.copy_(result[0].argmax(-1))
    dist.broadcast(tokens, src=STAGES - 1)
    generated = [tokens.tolist()]
    decode = schedule(1)
    with torch.no_grad():
        for step in range(GENERATED - 1):
            metadata = torch.tensor([[slot, length + step, 1] for slot, length in enumerate(lengths)])
            result = decode.step(tokens.unsqueeze(-1), metadata) if rank == 0 else decode.step()
            if rank == STAGES - 1:
                tokens.copy_(result[0].argmax(-1))
            dist.broadcast(tokens, src=STAGES - 1)
            generated.append(tokens.tolist())
    actual = torch.tensor(generated).transpose(0, 1).tolist()
    for slot, length in enumerate(lengths):
        assert module.caches[slot].get_seq_length(first) == length + GENERATED - 1
    snapshots = [None] * STAGES
    dist.all_gather_object(snapshots, module.spans)
    positions = [None] * STAGES
    dist.all_gather_object(positions, module.positions)
    if rank == STAGES - 1:
        assert actual == references, (actual, references)
        spans = [item for snapshot in snapshots for item in snapshot]
        concurrency = overlap(spans)
        assert concurrency["max_simultaneously_active_stages"] >= 2
        assert concurrency["overlap_ns"] >= 1_000_000
        # Reject stale/misrouted cache metadata explicitly.
        rejected = False
        try:
            module(torch.zeros(1, 1, cfg.hidden_size), torch.tensor([[0, 0, 1]]))
        except ValueError as error:
            rejected = "cache starts at" in str(error)
        assert rejected
        result = {
            "scope": "CPU only; actual AXK2 architecture with61 miniature random-weight blocks. NOT688B/checkpoint/GPU validation.",
            "scheduler": "PyTorch ScheduleGPipe forward-only, separate fixed-shape prefill/decode schedules",
            "transformers_revision": "7fb5bcd1d4b8a5c225a2c33429b2e9e023dd61ae",
            "processes": STAGES, "requests": REQUESTS, "layers": LAYERS,
            "prompt_lengths": lengths, "prefill_chunk": CHUNK, "generated_tokens": GENERATED,
            "prefill_vs_official_whole_model_max_error": prefill_error,
            "greedy_token_ids_equal_official_serial_model": actual == references,
            "all_stage_cache_positions": positions, "stale_cache_metadata_rejected": rejected,
            "cpu_compute_overlap": concurrency, "outputs": actual, "spans": spans, "passed": True,
        }
        (ROOT / "axk2-request-pipeline-cpu-result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({key: value for key, value in result.items() if key != "spans"}, indent=2))
    dist.destroy_process_group()


if __name__ == "__main__":
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    mp.spawn(worker, args=(port,), nprocs=STAGES, join=True)
