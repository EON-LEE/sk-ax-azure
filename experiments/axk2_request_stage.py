"""Request-isolated AXK2 stage for native PyTorch forward-only pipeline schedules."""
import time

import torch
from torch import nn
from transformers import DynamicCache


class AXK2RequestStage(nn.Module):
    def __init__(self, config, blocks, rotary, rank, stages, layer_start,
                 embedding=None, norm=None, head=None):
        super().__init__()
        assert blocks
        assert (embedding is not None) == (rank == 0)
        assert (norm is not None and head is not None) == (rank == stages - 1)
        self.config = config
        self.blocks = nn.ModuleList(blocks)
        self.rotary = rotary
        self.embedding, self.norm, self.head = embedding, norm, head
        self.rank, self.stages, self.layer_start = rank, stages, layer_start
        self.caches, self.positions, self.last_logits = {}, {}, {}
        self.spans = []

    def reset_requests(self):
        self.caches.clear()
        self.positions.clear()
        self.last_logits.clear()
        self.spans.clear()

    def forward(self, payload, metadata):
        # Fixed transport shapes; valid token counts exclude right-padding from caches.
        assert payload.shape[0] == metadata.shape[0] == 1
        assert metadata.shape[1] == 3
        slot, start, valid = [int(value) for value in metadata[0].tolist()]
        chunk = payload.shape[1]
        assert slot >= 0 and start >= 0 and 0 <= valid <= chunk
        expected = self.positions.get(slot, 0)
        if start != expected:
            raise ValueError(f"Stage{self.rank} request{slot}: cache starts at {expected}, received {start}")
        if not valid:
            if self.head is not None:
                if slot not in self.last_logits:
                    raise ValueError("Inactive request has no preceding logits")
                return self.last_logits[slot], metadata
            return torch.zeros(1, chunk, self.config.hidden_size, device=payload.device,
                               dtype=next(self.blocks.parameters()).dtype), metadata
        if slot not in self.caches:
            self.caches[slot] = DynamicCache(config=self.config)
        cache = self.caches[slot]
        begin = time.monotonic_ns()
        with torch.profiler.record_function(f"axk2.stage{self.rank}.request{slot}.position{start}"):
            x = self.embedding(payload[:, :valid]) if self.embedding is not None else payload[:, :valid]
            positions = torch.arange(start, start + valid, device=x.device).unsqueeze(0)
            keys = torch.arange(start + valid, device=x.device)
            allowed = keys[None, :] <= positions[0, :, None]
            mask = torch.zeros(1, 1, valid, start + valid, dtype=x.dtype, device=x.device)
            mask.masked_fill_(~allowed[None, None], torch.finfo(x.dtype).min)
            rope = self.rotary(x, positions)
            for block in self.blocks:
                x = block(hidden_states=x, position_embeddings=rope, attention_mask=mask,
                          past_key_values=cache, position_ids=positions, use_cache=True)
            if self.head is not None:
                result = self.head(self.norm(x[:, -1]))
                self.last_logits[slot] = result
            elif valid == chunk:
                result = x
            else:
                result = torch.cat((x, x.new_zeros(1, chunk - valid, self.config.hidden_size)), dim=1)
        self.positions[slot] = start + valid
        if cache.get_seq_length(self.layer_start) != start + valid:
            raise RuntimeError("Cache contains missing or padded tokens")
        self.spans.append({
            "rank": self.rank, "slot": slot, "position": start, "valid_tokens": valid,
            "start_ns": begin, "end_ns": time.monotonic_ns(),
            "scope": "CPU span; for CUDA this is a launch interval, NOT proof of GPU overlap.",
        })
        return result, metadata
