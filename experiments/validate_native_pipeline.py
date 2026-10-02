"""CPU scheduler test only: not AXK2 weights, GPUs, network capacity or quality."""
import json
import os
import socket
import time
from datetime import timedelta
from pathlib import Path

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch import nn
from torch.distributed.pipelining import PipelineStage, ScheduleGPipe

ROOT = Path(__file__).resolve().parent
STAGES, LAYERS, WIDTH, VOCAB, TOKENS, REQUESTS = 4, 61, 128, 512, 128, 8


class Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.norm = nn.LayerNorm(WIDTH)
        self.up = nn.Linear(WIDTH, WIDTH * 4)
        self.down = nn.Linear(WIDTH * 4, WIDTH)

    def forward(self, x):
        return x + self.down(torch.nn.functional.silu(self.up(self.norm(x)))) * 0.1


class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(VOCAB, WIDTH)
        self.blocks = nn.ModuleList(Block() for _ in range(LAYERS))
        self.norm = nn.LayerNorm(WIDTH)
        self.head = nn.Linear(WIDTH, VOCAB, bias=False)

    def forward(self, ids):
        x = self.embedding(ids)
        for block in self.blocks:
            x = block(x)
        return self.head(self.norm(x))


class Stage(nn.Module):
    def __init__(self, model, rank):
        super().__init__()
        self.rank = rank
        self.start = LAYERS * rank // STAGES
        self.end = LAYERS * (rank + 1) // STAGES
        self.embedding = model.embedding if rank == 0 else None
        self.blocks = nn.ModuleList(list(model.blocks)[self.start:self.end])
        self.norm = model.norm if rank == STAGES - 1 else None
        self.head = model.head if rank == STAGES - 1 else None
        self.spans = []

    def forward(self, x):
        begin = time.monotonic_ns()
        if self.embedding is not None:
            x = self.embedding(x)
        for block in self.blocks:
            x = block(x)
        if self.head is not None:
            x = self.head(self.norm(x))
        self.spans.append({"rank": self.rank, "start_ns": begin, "end_ns": time.monotonic_ns()})
        return x


def overlap(spans):
    events = sorted([(s["start_ns"], 1) for s in spans] + [(s["end_ns"], -1) for s in spans])
    active = maximum = simultaneous_ns = 0
    previous = events[0][0]
    for timestamp, change in events:
        if active >= 2:
            simultaneous_ns += timestamp - previous
        active += change
        maximum = max(maximum, active)
        previous = timestamp
    return {"max_simultaneously_active_stages": maximum, "overlap_ns": simultaneous_ns}


def worker(rank, port):
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.manual_seed(2026)
    dist.init_process_group("gloo", init_method=f"tcp://127.0.0.1:{port}",
                            rank=rank, world_size=STAGES, timeout=timedelta(minutes=3))
    model = Model().eval()
    module = Stage(model, rank).eval()
    input_ids = torch.randint(VOCAB, (REQUESTS, TOKENS))
    reference = None
    if rank == STAGES - 1:
        with torch.no_grad():
            reference = torch.cat([model(ids.unsqueeze(0)) for ids in input_ids])
    reports = []
    for mode, microbatches in (("serialized_control", 1), ("native_gpipe_overlap", REQUESTS)):
        sample_input = (torch.zeros(1, TOKENS, dtype=torch.long) if rank == 0
                        else torch.zeros(1, TOKENS, WIDTH))
        sample_output = torch.zeros(1, TOKENS, VOCAB if rank == STAGES - 1 else WIDTH)
        stage = PipelineStage(module, rank, STAGES, torch.device("cpu"),
                              input_args=sample_input, output_args=sample_output)
        schedule = ScheduleGPipe(stage, n_microbatches=microbatches, loss_fn=None)
        assert not schedule._has_backward, "This must be inference, not a training schedule"
        with torch.no_grad():
            if rank == 0:
                schedule.step(input_ids[:microbatches])
            else:
                schedule.step()
        dist.barrier()
        module.spans.clear()
        outputs = []
        begin = time.monotonic_ns()
        with torch.no_grad():
            for offset in range(0, REQUESTS, microbatches):
                value = (schedule.step(input_ids[offset:offset + microbatches]) if rank == 0
                         else schedule.step())
                if rank == STAGES - 1:
                    outputs.append(value)
                if microbatches == 1:
                    dist.barrier()
        dist.barrier()
        elapsed = time.monotonic_ns() - begin
        assert len(module.spans) == REQUESTS
        all_spans = [None] * STAGES
        dist.all_gather_object(all_spans, module.spans)
        if rank == STAGES - 1:
            actual = torch.cat(outputs)
            torch.testing.assert_close(actual, reference, atol=1e-5, rtol=1e-5)
            spans = [span for group in all_spans for span in group]
            report = {"mode": mode, "elapsed_ns": elapsed, "spans": spans,
                      "max_absolute_error": (actual - reference).abs().max().item(), **overlap(spans)}
            if microbatches == 1:
                assert report["max_simultaneously_active_stages"] == 1
            else:
                assert report["max_simultaneously_active_stages"] >= 2
                assert report["overlap_ns"] >= 1_000_000, "Require observed compute overlap, not nonblocking API usage alone"
            reports.append(report)
    if rank == STAGES - 1:
        result = {
            "scope": "Four local CPU processes, small random residual model; NOT real AXK2 or a GPU result.",
            "torch": torch.__version__, "pid": os.getpid(),
            "scheduler": "torch.distributed.pipelining.ScheduleGPipe, loss_fn=None (forward only)",
            "stages": STAGES, "layers": LAYERS, "requests": REQUESTS,
            "coverage": "All61 small test blocks, embedding, final normalization and head. This tests scheduling only.",
            "timing": "Same-host monotonic CPU compute spans. No GPU speedup or multi-VM timing claim.",
            "cases": reports, "passed": True,
        }
        (ROOT / "native-pipeline-cpu-result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({**result, "cases": [{k: v for k, v in r.items() if k != "spans"} for r in reports]}, indent=2))
    dist.destroy_process_group()


if __name__ == "__main__":
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    mp.spawn(worker, args=(port,), nprocs=STAGES, join=True)
