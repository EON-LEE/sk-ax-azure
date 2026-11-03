# vLLM configurations for A.X-K2 on A100

Snapshot: 2026-10-22. This catalog lists every vLLM configuration that this
repository has served for A.X-K2 on A100: the two demo profiles, the layouts of
the expert-parallel comparison, and the other configurations recorded in
[SERVING_DESIGN.md](SERVING_DESIGN.md) ("design doc" below). Each entry copies
facts from the design doc and `evidence/` and names its source section.

## Operating status (2026-10-22)

As of 2026-10-22 the demo GPUs are off. Both demo profiles, FP8 and NVFP4, were
switched off at the user's request: their last jobs are cancelled and the GPU
cluster has no nodes, so no GPU is billed. To serve again, switch the
supervisors on: `demo/ctl.sh on` for FP8 and the NVFP4 profile's admin power
switch (`/models/nvfp4/api/admin/power`), or
`demo/target.py --directory <target-directory> on --keep-on` for both
(`demo/README.md`). The FP8 profile is ready about 20-30 minutes after power-on
when nodes are free: 25 minutes in `demo/README.md`, and 22 and 32 minutes in
the two recoveries of design doc 9. Most of that time is the 694 GB checkpoint
download (about 10-12 minutes per node on 2026-10-21) and about 4 minutes of
server start-up. The one-node NVFP4 layout took about 15 minutes of download and
start-up on 2026-10-21 (459 s and 442 s). The cluster is
low-priority (Spot) with no reserved capacity, so nodes can come back late or
not at all: on 2026-10-21 AML released the three freed nodes and took about ten
minutes to allocate new ones, and earlier searches found two free nodes in only
two of twelve regions (design doc 9). Separately, a governance automation in
the subscription has been moving the demo web app to the F1 tier with Always On
off every day. Always On needs the Basic tier or higher, and App Service unloads
an idle app without it. That also stops the supervisors and the reverse GPU
links, which run in the web app process (design doc 8.5, 8.6), and demo jobs
download their scripts from the web app. Check the plan tier and Always On
before a demo.

## Status labels and measurement rules

| Label | Meaning |
| --- | --- |
| Current demo | A layout the demo serves. |
| Tested, not recommended | Started and passed the functional and tool-calling checks, but on balance measured worse than the demo layout in the same allocation (see its row). |
| Throughput only | Faster than the demo layout only with many concurrent requests, and slower for a single request. |
| Tested alternative | Works, with a documented trade-off against the demo layout. |
| Record under other conditions | Measured with other settings; not comparable with the expert-parallel comparison. |
| Validation only | Used to check correctness or a feature, not to serve. |
| Cannot start on A100 | vLLM 0.23 refuses the configuration on this GPU. |

- Speed numbers come from `vllm bench serve` with random prompts and
  `--ignore-eos`. Each point was run once (single sample), so a difference of a
  few percent may be noise.
- Ratios compare layouts measured in the same allocation. Numbers from
  different sections of the design doc were taken under different conditions
  (checkpoint label, context, sequences, batched tokens, scheduling,
  interconnect), so they are not compared with each other.

## Summary

| ID | Status | Checkpoint | GPUs | Layout | Executor | DSA | Job template, phase | Design doc |
| --- | --- | --- | ---: | --- | --- | --- | --- | --- |
| `demo-fp8` | Current demo (off) | FP8 `4237c6a6` | 16 | TP8 x PP2 | Ray | native | `demo-fp8-nd96.yml`, `demo` | 8.1, 8.5, 8.7 (A) |
| `demo-nvfp4` | Current demo (off) | NVFP4 `9e2e804e` | 8 | TP8 | mp | native | `demo-nvfp4-nd96.yml`, `demo` | 8.4, 8.5, 8.7 (D) |
| `epc-fp8-tp16ep16` | Tested, not recommended | FP8 `4237c6a6` | 16 | TP16 + EP (EP16) | Ray | native | `ep-compare-fp8-nd96.yml`, same name | 8.7 (B) |
| `epc-fp8-tp8pp2ep8` | Tested, not recommended | FP8 `4237c6a6` | 16 | TP8 x PP2 + EP (EP8 per stage) | Ray | native | `ep-compare-fp8-nd96.yml`, same name | 8.7 (C1) |
| `epc-fp8-tp16` | Tested, not recommended | FP8 `4237c6a6` | 16 | TP16 | Ray | native | `ep-compare-fp8-nd96.yml`, same name | 8.7 (B0) |
| `epc-fp8-dp2tp8ep16` | Throughput only | FP8 `4237c6a6` | 16 | DP2 x TP8 + EP (EP16) | Ray | native | `ep-compare-fp8-nd96.yml`, same name | 8.7 (C2) |
| `epc-nvfp4-tp8ep8` | Tested, not recommended | NVFP4 `9e2e804e` | 8 | TP8 + EP (EP8) | mp | native | `ep-compare-nvfp4-nd96.yml`, same name | 8.7 (E) |
| `dense-pp2` | Tested alternative (contexts up to about 60K tokens) | FP8, measured on `2287ca45` | 16 | TP8 x PP2 | Ray | dense | `verify-remaining-nd96-hub.yml`, `dense-pp2` | 5, 8.1-8.3 |
| `dense-tp16` | Record under other conditions | FP8, measured on `2287ca45` | 16 | TP16 | Ray | dense | `verify-remaining-nd96-hub.yml`, `dense-tp16` | 8.1 |
| `dense-tp16-eagle3` | Validation only (not usable) | FP8 `2287ca45` + EAGLE3 `24958e91` | 16 | TP16 + EAGLE3 drafter | Ray | dense | `verify-remaining-nd96-hub.yml`, `dense-tp16-eagle3` | 8.1 |
| `native-pp2-bi`, `dense-pp2-bi` | Cannot start on A100 | FP8, tried on `2287ca45` | 16 | TP8 x PP2, batch-invariant mode | Ray | native, dense | `verify-remaining-nd96-hub.yml`, same names | 8.1 |
| `nvfp4-tp8-b2048` | Tested alternative | NVFP4 `9e2e804e` | 8 | TP8, 2,048 batched tokens | mp | native | `nvfp4-a100-nd96.yml`, `nvfp4-tp8-b2048` | 8.4 |
| 2-layer cut | Validation only | FP8 2-layer cut | 8, then 16 | TP8, then TP8 x PP2 | vLLM default, then Ray | native, dense | `native-dsa-bench-nd96-hub.yml`, smoke phases | 7, 8.2, 8.3 |

## Common engine and command

- **Engine.** Every entry runs `docker.io/vllm/vllm-openai:v0.23.0-cu129`
  (vLLM 0.23.0). At job start `aml/src/apply_overlay.py` adds the SKT fork
  `axk2-v0.23.0` @ `023bc760`, the port of upstream PR #38476 (the
  `TRITON_MLA_SPARSE` backend and the Triton indexer that native DSA uses) and
  the two-line Ampere MLA fix (design doc 4, 5b). The NVFP4 entries also apply
  `aml/src/nvfp4_sm80_port.json` (`NVFP4_SM80_PORT=1`, design doc 8.4). On A100
  the FP8 linear and MoE layers run as Marlin W8A16, the NVFP4 experts as
  Marlin W4A16, and the KV cache is BF16.
- **Hardware.** Standard_ND96amsr_A100_v4 nodes (8 x A100 80GB with NVLink,
  InfiniBand between nodes) in an Azure ML low-priority (Spot) cluster of at
  most three nodes. The FP8 demo takes two nodes and the NVFP4 demo one, so
  with both on the cluster is full (design doc 8.5).
- **Checkpoints.** FP8: `skt/A.X-K2` @ `4237c6a60cec6ae449854870d01a42665a1166aa`.
  The Hub has returned 404 for the earlier pin `2287ca45` since 2026-10-20.
  `4237c6a6` matches it in file count, total bytes and the nine shard hashes
  recorded for `2287ca45`. PR #35 records that check and re-pins the demo job;
  until it is merged, `aml/jobs/demo-fp8-nd96.yml` on main and on this branch
  still names `2287ca45`. The runs of design doc 8.1-8.3 measured `2287ca45`,
  and the expert-parallel comparison used `4237c6a6`. NVFP4:
  `skt/A.X-K2-NVFP4` @ `9e2e804e80f8d1b3afba5d7938173cec1ed46b49`. EAGLE3
  drafter: `skt/A.X-K2-EAGLE3` @ `24958e91737d760908f73a8af4b6e06080fc5c1d`.
- **DSA mode.** Native mode serves the checkpoint unchanged; the indexer selects
  the top 2,048 keys per query. Dense mode serves a copy of the checkpoint
  directory whose config lacks `index_topk`, `index_n_heads` and
  `index_head_dim` (`aml/src/make_dense_dir.py`). Dense mode equals DSA only
  while the context fits in 2,048 tokens (design doc 5).
- **Command.** `aml/src/entry.sh` starts every server as

  ```bash
  vllm serve <model dir> --served-model-name axk2 --host 0.0.0.0 --port 8000 \
    --max-model-len <len> --gpu-memory-utilization <util> --max-num-seqs <seqs> \
    --reasoning-parser deepseek_v3 --no-enable-log-requests <phase flags>
  ```

  The `PHASE_PLAN` phases (`plan_phase`) add `--tool-call-parser hermes
  --enable-auto-tool-choice`, `--max-num-batched-tokens 8192` (2,048 in
  `nvfp4-tp8-b2048`) and their layout flags. The `epc-*` phases also add
  `--scheduling-policy priority`. The demo phase adds the same parser flags,
  8,192 batched tokens and priority scheduling. The older `MODES` phases
  (`smoke-*`, `full-*`) add only their layout flags. If a server does not
  become healthy with CUDA graphs, `entry.sh` retries once with
  `--enforce-eager` and reports it as `eager_fallback`. The full command of the
  FP8 demo, for reference:

  ```bash
  vllm serve <checkpoint dir> --served-model-name axk2 --host 0.0.0.0 --port 8000 \
    --tensor-parallel-size 8 --pipeline-parallel-size 2 --distributed-executor-backend ray \
    --max-model-len 262144 --max-num-seqs 64 --gpu-memory-utilization 0.90 \
    --max-num-batched-tokens 8192 --scheduling-policy priority \
    --reasoning-parser deepseek_v3 --tool-call-parser hermes --enable-auto-tool-choice \
    --no-enable-log-requests
  ```

- **Multi-node jobs.** Node 0 starts a Ray head and the other node joins it.
  With `IB_PROBE=1` both nodes first measure NCCL over TCP and over InfiniBand,
  and serving uses InfiniBand only if NCCL chose it (design doc 8.1). The runs
  of design doc 8.2 and 8.3 predate the probe and used TCP.

## Current demo

### `demo-fp8`: FP8, TP8 x PP2 over two nodes

| Field | Value |
| --- | --- |
| Status | Current demo; off since 2026-10-22 |
| Checkpoint | `skt/A.X-K2` @ `4237c6a6` (FP8, block-scaled), as deployed; the template on this branch still names `2287ca45` (see Checkpoints) |
| GPUs | 2 nodes, 16 x A100 80GB |
| Parallel flags | `--tensor-parallel-size 8 --pipeline-parallel-size 2 --distributed-executor-backend ray`; layers 0-30 on node 0, layers 31-60 and the LM head on node 1 |
| Other flags | `--max-model-len 262144 --max-num-seqs 64 --gpu-memory-utilization 0.90 --max-num-batched-tokens 8192 --scheduling-policy priority`, parser flags as above |
| Executor, DSA | Ray; native |
| Job, env | `aml/jobs/demo-fp8-nd96.yml`, `PHASE_PLAN=demo`, `IB_PROBE=1`, `TP=8`, `PP=2`, `FULL_MAX_MODEL_LEN=262144`, `FULL_MAX_NUM_SEQS=64`, `FULL_GPU_MEM_UTIL=0.90`, `DEMO_RESTARTS=2`, `RUN_KERNEL_TESTS=0`. The supervisor fills in `AXK2_LINK_URL`, `AXK2_LINK_TOKEN`, `AXK2_SRC_URL` and `AXK2_SRC_SHA256` for each submission. |
| Context, KV cache | 262,144 tokens; 673,792 tokens (42.3 GiB weights and 25.0 GiB KV per GPU) |
| Start-up | 261 s to healthy as baseline A on 2026-10-21; 262 s in design doc 8.1 |
| Measured (design doc 8.7, baseline A, single sample) | Output tok/s at concurrency 1 / 32: 48 / 404 with 1K input, 42 / 208 with 8K, 32 / 61 with 32K. Median TPOT for one request 20.7-22.7 ms. With 32K input and concurrency 32, 20 requests run at once. Functional 4/4, tools 9/9. |
| Earlier records (other conditions) | Design doc 8.1 (`native-pp2`, `2287ca45`, no priority scheduling): tools 9/9, SKT's needle test 9/9 up to 252,728 tokens, 0.43-0.51x of one B200 node at 1K-8K input under the tech report's Fig. 7 conditions. Design doc 8.2 (65,536 context, 128 sequences, vLLM's A100 default of 2,048 batched tokens, TCP): healthy after 302 s, KV cache 724,544 tokens. |
| Caveats | The supervisor renders and submits this job; do not submit it by hand. Output is not bitwise deterministic, and batch-invariant mode cannot start (design doc 8.1). |

### `demo-nvfp4`: NVFP4, TP8 on one node

| Field | Value |
| --- | --- |
| Status | Current demo; off since 2026-10-22 |
| Checkpoint | `skt/A.X-K2-NVFP4` @ `9e2e804e` (ModelOpt mixed precision: NVFP4 routed experts, FP8 attention and shared experts) |
| GPUs | 1 node, 8 x A100 80GB |
| Parallel flags | `--tensor-parallel-size 8 --pipeline-parallel-size 1 --distributed-executor-backend mp` |
| Other flags | `--max-model-len -1 --max-num-seqs 128 --gpu-memory-utilization 0.92 --max-num-batched-tokens 8192 --scheduling-policy priority`, parser flags as above |
| Executor, DSA | mp (multiprocessing, no Ray); native |
| Job, env | `aml/jobs/demo-nvfp4-nd96.yml`, `PHASE_PLAN=demo`, `DEMO_EXECUTOR=mp`, `FULL_REPO=skt/A.X-K2-NVFP4`, `NVFP4_SM80_PORT=1`, `TP=8`, `PP=1`, `FULL_MAX_MODEL_LEN=-1`, `FULL_MAX_NUM_SEQS=128`, `FULL_GPU_MEM_UTIL=0.92`, `DEMO_RESTARTS=2`. The NVFP4 supervisor fills in the link and source values. |
| Context, KV cache | Auto-fitted to 254,016 tokens; 254,016 tokens (48.5 GiB weights and 18.5 GiB KV per GPU) |
| Start-up | 442 s to healthy as baseline D on 2026-10-21; 433 s in design doc 8.4 |
| Measured (design doc 8.7, baseline D, single sample) | Output tok/s at concurrency 1 / 32: 48 / 445 with 1K input, 42 / 188 with 8K, 27 / 51 with 32K. Median TPOT for one request 20.4-22.4 ms. With 32K input and concurrency 32, 7 requests run at once. Functional 4/4, tools 9/9. |
| Earlier records (other conditions) | Design doc 8.4 (`nvfp4-tp8`, no priority scheduling): needles 9/9 up to 244,898 tokens; 0.47-0.49x of one B200 node at 1K-4K input under the Fig. 7 conditions. |
| Caveats | The experts run W4A16 (A100 has no FP4 tensor cores). The engine needs a backport of two vLLM 0.24 changes. Quality was checked only with functional checks, tool calls and needles (design doc 8.4). |

## Expert-parallel comparison (2026-10-21, design doc 8.7)

**Shared settings.** Native DSA. FP8 `4237c6a6` with 64 sequences at 0.90 GPU
memory, and NVFP4 `9e2e804e` with 128 sequences at 0.92 (the demo values). Every
layout adds `--max-model-len -1 --max-num-batched-tokens 8192
--scheduling-policy priority` and the parser flags. The jobs are:

- `aml/jobs/ep-compare-fp8-nd96.yml`:
  - `PHASE_PLAN="epc-fp8-tp8pp2 epc-fp8-tp16ep16 epc-fp8-tp8pp2ep8 epc-fp8-tp16 epc-fp8-dp2tp8ep16"`;
  - `IB_PROBE=1`, `TP=8`, `PP=2`, `FULL_MAX_MODEL_LEN=-1`, `FULL_MAX_NUM_SEQS=64`,
    `FULL_GPU_MEM_UTIL=0.90`;
  - a 5-hour `limits.timeout`.
- `aml/jobs/ep-compare-nvfp4-nd96.yml`:
  - `PHASE_PLAN="epc-nvfp4-tp8 epc-nvfp4-tp8ep8"`;
  - `FULL_REPO=skt/A.X-K2-NVFP4`, `NVFP4_SM80_PORT=1`, `TP=8`, `PP=1`,
    `FULL_MAX_MODEL_LEN=-1`, `FULL_MAX_NUM_SEQS=128`, `FULL_GPU_MEM_UTIL=0.92`;
  - a 3-hour `limits.timeout`.

`epc-fp8-tp8pp2` (A) and `epc-nvfp4-tp8` (D) are the demo layouts, measured again
as baselines in the same allocations.

`--enable-expert-parallel` is a switch, not a size. vLLM 0.23 makes each MoE
layer expert parallel over the DP x TP ranks of its pipeline stage and runs it
with TP 1. Attention and the shared expert keep their layout (design doc 8.7).
No layout needed a code change. Every layout started with CUDA graphs and passed
4/4 functional checks and 9/9 tool calls.

| Phase (label in 8.7) | Status | Parallel flags | EP size, experts per GPU | Context / KV cache tokens | Weights / KV per GPU | Healthy after |
| --- | --- | --- | --- | --- | --- | ---: |
| `epc-fp8-tp8pp2` (A) | Baseline, = `demo-fp8` | `--tensor-parallel-size 8 --pipeline-parallel-size 2 --distributed-executor-backend ray` | none | 262,144 / 673,792 | 42.3 / 25.0 GiB | 261 s |
| `epc-fp8-tp16ep16` (B) | Tested, not recommended | `--tensor-parallel-size 16 --pipeline-parallel-size 1 --distributed-executor-backend ray --enable-expert-parallel` | EP16, 16 of 256 | 262,144 / 309,376 | 43.3 / 22.6 GiB | 231 s |
| `epc-fp8-tp8pp2ep8` (C1) | Tested, not recommended | `--tensor-parallel-size 8 --pipeline-parallel-size 2 --distributed-executor-backend ray --enable-expert-parallel` | EP8 in each stage, 32 | 262,144 / 673,216 | 42.3 / 25.0 GiB | 151 s |
| `epc-fp8-tp16` (B0) | Tested, not recommended (control for B) | `--tensor-parallel-size 16 --pipeline-parallel-size 1 --distributed-executor-backend ray` | none | 262,144 / 310,080 | 43.3 / 22.6 GiB | 251 s |
| `epc-fp8-dp2tp8ep16` (C2) | Throughput only | `--tensor-parallel-size 8 --pipeline-parallel-size 1 --data-parallel-size 2 --data-parallel-size-local 1 --data-parallel-backend ray --enable-expert-parallel` | EP16, 16 | 238,656 / 238,720 + 238,656 (two engines) | 46.4 / 17.4 GiB | 221 s |
| `epc-nvfp4-tp8` (D) | Baseline, = `demo-nvfp4` | `--tensor-parallel-size 8 --pipeline-parallel-size 1 --distributed-executor-backend mp` | none | 254,016 / 254,016 | 48.5 / 18.5 GiB | 442 s |
| `epc-nvfp4-tp8ep8` (E) | Tested, not recommended | `--tensor-parallel-size 8 --pipeline-parallel-size 1 --distributed-executor-backend mp --enable-expert-parallel` | EP8, 32 | 253,120 / 253,120 | 48.5 / 18.5 GiB | 312 s |

**Measured (single sample per point).** Absolute values for the baselines, and
ratios to A (FP8) or D (NVFP4) for the others. Inputs of 1K / 8K / 32K tokens,
1,024 output tokens.

| Phase | Output tok/s, concurrency 1 | Output tok/s, concurrency 32 | Median TPOT, one request | Requests running, 32K input at concurrency 32 |
| --- | --- | --- | --- | ---: |
| A `epc-fp8-tp8pp2` | 48 / 42 / 32 | 404 / 208 / 61 | 20.7-22.7 ms | 20 |
| B `epc-fp8-tp16ep16` | 0.70 / 0.72 / 0.70 | 0.98 / 0.98 / 0.83 | 1.40-1.44x | 9 |
| C1 `epc-fp8-tp8pp2ep8` | 0.88 / 0.89 / 0.91 | 0.92 / 0.92 / 0.92 | 1.12-1.13x | 20 |
| B0 `epc-fp8-tp16` | 0.79 / 0.82 / 0.77 | 1.04 / 1.04 / 0.89 | 1.24-1.26x | 9 |
| C2 `epc-fp8-dp2tp8ep16` | 0.50 / 0.54 / 0.56 | 1.12 / 1.36 / 1.18 | 1.88-1.99x | 14 (both engines) |
| D `epc-nvfp4-tp8` | 48 / 42 / 27 | 445 / 188 / 51 | 20.4-22.4 ms | 7 |
| E `epc-nvfp4-tp8ep8` | 0.93 / 0.93 / 0.93 | 0.95 / 0.93 / 0.94 | 1.08x | 7 |

**Caveats per layout.**

- B and B0: TP16 spans both nodes, so vLLM disables its custom all-reduce and
  every layer's all-reduce uses NCCL over InfiniBand. The MLA cache is
  replicated on 16 ranks that each hold all 61 layers, so the KV cache holds
  0.46x of A's tokens.
- C1 and E: with DP = 1, EP dispatches nothing. Each rank computes its own
  experts and the outputs are all-reduced as without EP. Output tok/s was
  8-12% (C1) and 5-7% (E) below the baseline.
- C2:
  - The log says "Using ray distributed inference because data_parallel_backend
    is ray". DP rank 0 runs on node 0 and DP rank 1 on node 1.
  - Two API server processes (`ApiServer_0`, `ApiServer_1`) served the one
    endpoint.
  - MoE dispatch uses allgather/reduce-scatter over InfiniBand
    (`AgRsAll2AllManager`).
  - At 8K input and concurrency 32 the median TTFT is 1.56x of A, and the
    context shrinks to 238,656 tokens.
- DeepEP is in the image but built only for Hopper and Blackwell, so it does
  not run on A100. EPLB, `--enable-ep-weight-filter` and round-robin expert
  placement were not tested.

## Other configurations in the design doc

These were measured from 2026-10-02 to 2026-10-04 with the FP8 checkpoint
`2287ca45` or the NVFP4 checkpoint `9e2e804e`, without priority scheduling, and
with the settings listed per entry. They are records under their own
conditions. Do not compare them with the expert-parallel comparison above, or
with each other across sections.

### `dense-pp2`: FP8 dense mode, TP8 x PP2 (design doc 5, 8.1-8.3)

| Field | Value |
| --- | --- |
| Status | Tested alternative, for contexts up to about 60K tokens only |
| Layout | The demo's layout (`--tensor-parallel-size 8 --pipeline-parallel-size 2 --distributed-executor-backend ray`), serving the dense copy of the checkpoint (`/tmp/axk2-full-dense`, made by `make_dense_dir.py`). Decode uses `TRITON_MLA`, prefill FlashAttention-2 MLA. |
| Jobs | `verify-remaining-nd96-hub.yml`, phase `dense-pp2` (`IB_PROBE=1`, context 262,144, 64 sequences, 0.90, 8,192 batched tokens). `native-dsa-bench-nd96-hub.yml` and `full-native-dense-nd96-hub.yml`, phase `full-dense-tp8-pp2` from `MODES="native dense"` (context 65,536, 128 sequences, vLLM's A100 default of 2,048 batched tokens). |
| Start-up, KV cache | 8.1: 181 s, 766,032 tokens. 8.2: 171 s, 821,312 tokens (41.7 GiB weights and 26.8 GiB KV per GPU), over TCP. 8.3: 181 s, 843,920 tokens, over TCP. |
| Measured (single sample per point) | 8.1, the same allocation as native TP8 x PP2, tech report Fig. 7 conditions (concurrency 32, 1,024 output tokens): 1.2-2.4x the total tok/s of native from 1K to 120K input. 8.2: 1.1-1.6x the output tok/s of native. Functional 6/6 (8.2), tools 9/9 (8.1). |
| Caveats | Exact only up to 2,048 context tokens (design doc 5). Needles were found up to 60K tokens (12/12 in 8.2) and at 32K (3/3 in 8.1), but none at 126K or 253K tokens (0/6 in 8.1). Use native DSA for longer contexts. |

### `dense-tp16`: FP8 dense mode, TP16 over two nodes (design doc 8.1)

| Field | Value |
| --- | --- |
| Status | Record under other conditions. It ran in dense mode with a 32,768-token context and is not the TP16 layout of the expert-parallel comparison: for native TP16, see `epc-fp8-tp16` (B0) and `epc-fp8-tp16ep16` (B). |
| Layout | `--tensor-parallel-size 16 --pipeline-parallel-size 1 --distributed-executor-backend ray`, dense checkpoint |
| Job | `verify-remaining-nd96-hub.yml`, phase `dense-tp16` (context 32,768, 64 sequences, 0.90, 8,192 batched tokens, `IB_PROBE=1`) |
| Start-up, KV cache | 241 s; 362,848 tokens |
| Measured (single sample per point) | Against `dense-pp2` in the same allocation, concurrency 32 and 1,024 output tokens: total tok/s 1.41x at 1K input, 1.20x at 4K and 1.12x at 8K (1,386 / 2,638 / 3,449 against 980 / 2,202 / 3,070). Functional 4/4. |
| Caveats | The KV cache holds 0.47x the tokens of `dense-pp2`, so the layout suits short contexts only. All the dense-mode caveats apply. |

### `dense-tp16-eagle3`: TP16 with the EAGLE3 drafter (design doc 8.1)

| Field | Value |
| --- | --- |
| Status | Validation only; not usable on A100 |
| Layout | The `dense-tp16` flags plus `--speculative-config '{"method": "eagle3", "model": <drafter dir>, "num_speculative_tokens": 3}'`, with the drafter `skt/A.X-K2-EAGLE3` @ `24958e91` (`EAGLE3_SOURCE`) |
| Job | `verify-remaining-nd96-hub.yml`, phase `dense-tp16-eagle3` |
| Start-up, KV cache | The CUDA-graph start failed at `assert m.max_query_len <= self.reorder_batch_threshold`. The eager retry was healthy after 131 s with 367,904 KV tokens. |
| Measured (single sample per point) | Eager: 324 / 595 / 643 total tok/s at 1K / 4K / 8K input and concurrency 32, which is 0.19-0.23x of `dense-tp16` with CUDA graphs. It accepted 1.05 tokens per step, and 4/4 functional checks passed. |
| Caveats | `TRITON_MLA`, the only MLA decode kernel on SM80, verifies one token per request, so drafts fall back to the prefill path. EAGLE3 cannot run with PP (the drafter reads layers 2, 30 and 58). With native DSA it was not tried. |

### `native-pp2-bi` and `dense-pp2-bi`: batch-invariant mode (design doc 8.1)

| Field | Value |
| --- | --- |
| Status | Cannot start on A100 |
| Layout | TP8 x PP2 with `VLLM_BATCH_INVARIANT=1`, context 32,768, 32 sequences, in native and dense mode |
| Job | `verify-remaining-nd96-hub.yml`, phases `native-pp2-bi` and `dense-pp2-bi` |
| Result | Start-up fails with "No FP8 MoE backend supports the deployment configuration". The only batch-invariant MoE kernel is the Triton MoE kernel, and its FP8 path needs compute capability 8.9. Without this mode, identical greedy requests diverge after 68-264 characters. |

### `nvfp4-tp8-b2048`: NVFP4 TP8 with 2,048 batched tokens (design doc 8.4)

| Field | Value |
| --- | --- |
| Status | Tested alternative |
| Layout | The `demo-nvfp4` layout (`--tensor-parallel-size 8 --distributed-executor-backend mp`, `--max-model-len -1`, 128 sequences, 0.92) with `--max-num-batched-tokens 2048` and without priority scheduling |
| Job | `nvfp4-a100-nd96.yml`, phase `nvfp4-tp8-b2048`, after phase `nvfp4-tp8` (8,192 batched tokens) in the same allocation (`FULL_REPO=skt/A.X-K2-NVFP4`, `NVFP4_SM80_PORT=1`) |
| Start-up, KV cache | 352 s; 272,960 tokens (19.91 GiB per GPU), so the full 262,144-token context fits. With 8,192 batched tokens: 433 s and 254,016 tokens. |
| Measured (single sample per point) | Against `nvfp4-tp8` in the same allocation, on the 256-output-token latency plan of design doc 8.4: output tok/s 0.97-1.07x, median TPOT 19.0 ms against 20.4 ms for one 1K request, and 655 against 673 output tok/s at concurrency 128 with 1K input. |
| Caveats | vLLM uses 2,048 batched tokens by default on A100. The demo and the expert-parallel comparison use 8,192, the tech report's B200 setting. The earlier FP8 table of design doc 8.2 used 2,048. |

### 2-layer cut: correctness rehearsal (design doc 7, 8.2, 8.3)

| Field | Value |
| --- | --- |
| Status | Validation only |
| Layout | The byte-exact 2-layer cut of the FP8 checkpoint (`hf-smoke:<revision>`, `build_smoke_checkpoint.py`): one node TP8 in native and dense mode with the vLLM default executor, then TP8 x PP2 over Ray in native mode. Context 16,384, 16 sequences, 0.85, `--no-enable-prefix-caching`. |
| Job | `native-dsa-bench-nd96-hub.yml` (`RUN_KERNEL_TESTS=1`), phases `smoke-native-tp8`, `smoke-dense-tp8` and `smoke-native-tp8-pp2` |
| Measured | Mean absolute log-probability difference of the chosen token against the official Transformers FP32 sparse reference, inside / beyond `index_topk`: native TP8 0.0116 / 0.0111, native TP8 x PP2 0.0120 / 0.0115, dense TP8 0.0116 / 0.0123, on 14,618 positions (8.2). The 94 kernel tests of PR #38476 passed. |
| Caveats | Two layers bound the size of the effect; the full model cannot run in the FP32 CPU reference. |

### Not in this catalog

- The hand-written block runtime in `experiments/axk2_dist_*` is not a vLLM
  configuration. It is kept only as a numerical oracle (design doc 1).
- The model-only API (design doc 8.6) is a web-app path to the demo servers, not
  a separate vLLM configuration.
- Not tested: NVFP4 across two nodes (design doc 8.4), EAGLE3 with native DSA
  (8.1), data-parallel attention without TP (DP16 x TP1 + EP), EPLB and DeepEP
  (8.7, 9).

## Reproduce a configuration

**How a job gets its scripts.** Templates carry `aml/src` (`entry.sh` and its
helpers) in one of three ways:

| Placeholder | Templates | How to run |
| --- | --- | --- |
| `__AXK2_SRC_URL__` | `demo-fp8-nd96.yml`, `demo-nvfp4-nd96.yml` | The web app's supervisor renders and submits these with a fresh link token per submission, and the job downloads its scripts from the web app (`/api/link/src`, checked against `AXK2_SRC_SHA256`). Start them with the supervisor (see Operating status), not by hand. A demo job can start only while the web app is up. |
| `__AXK2_SRC_GIT__` | `ep-compare-fp8-nd96.yml`, `ep-compare-nvfp4-nd96.yml` | Commit and push `aml/src` first. `render_job.py` pins the pushed HEAD, and the job downloads each file from GitHub at that commit and checks its SHA-256. Rendering refuses uncommitted or unpushed `aml/src`. |
| `__AXK2_SRC_B64__` | `verify-remaining-nd96-hub.yml`, `nvfp4-a100-nd96.yml`, `native-dsa-bench-nd96-hub.yml`, `full-native-dense-nd96-hub.yml`, `serve-tp8-pp2-nd96.yml` and others | `aml/src` travels base64-encoded in an environment variable. It is now 96,292 characters, above the size that failed with `ArgumentTooLong` (about 91K, design doc 6). Before running one of these again, switch it to the `__AXK2_SRC_GIT__` command of the `ep-compare` templates (not tried). |

**Checkpoint pin.** Templates that still name `hf:2287ca45...` fail to
download, because the Hub returns 404 for that revision. Change them to
`hf:4237c6a60cec6ae449854870d01a42665a1166aa`. PR #35 does this for
`demo-fp8-nd96.yml`.

**One layout per job.** Every `epc-*`, `dense-*`, `native-*` and `nvfp4-*`
phase of this catalog is a case in `plan_phase` of `aml/src/entry.sh`. To run
one of them, copy the template of its checkpoint and node count, set
`PHASE_PLAN` to the phase name, and set the variables listed for the phase,
such as `FULL_MAX_MODEL_LEN` or `EAGLE3_SOURCE`. Use
`ep-compare-fp8-nd96.yml` for the two-node FP8 phases. Use
`ep-compare-nvfp4-nd96.yml` (with `FULL_REPO` and `NVFP4_SM80_PORT=1`) for
the one-node NVFP4 phases. A phase serves, runs its suites, reports to MLflow
and stops its server, and the job ends after the last phase. Only
`PHASE_PLAN=demo` (`demo_phase`) keeps serving until the job is cancelled. The
`smoke-*` phases run when the job's first argument names a smoke model, and the
`full-*` phases come from `MODES` when `PHASE_PLAN` is empty.

**Commands.** Run from WSL or Linux with the Azure ML CLI v2, and name the
subscription on every call.

```bash
python3 aml/render_job.py aml/jobs/ep-compare-fp8-nd96.yml   # writes aml/jobs/.rendered/, which git ignores
az ml job create --subscription <subscription-id> -g <resource-group> -w <workspace> \
  -f aml/jobs/.rendered/ep-compare-fp8-nd96.yml --query name -o tsv
az ml job show --subscription <subscription-id> -g <resource-group> -w <workspace> -n <job-name> \
  --query status -o tsv
az ml job cancel --subscription <subscription-id> -g <resource-group> -w <workspace> -n <job-name>   # stops billing

# Results are MLflow tags, because the workspace storage is private (design doc 6).
export AXK2_MLFLOW_URI=$(az ml workspace show --subscription <subscription-id> -g <resource-group> \
  -n <workspace> --query mlflow_tracking_uri -o tsv | sed 's#^azureml://#https://#')
export MLFLOW_TRACKING_TOKEN=$(az account get-access-token --subscription <subscription-id> \
  --query accessToken -o tsv)
python3 aml/fetch_results.py show <job-name>   # needs mlflow and azureml-mlflow
```

The two `export` lines matter when the CLI's default account is in another
subscription. With the `azureml://` URI, the MLflow plugin authenticates as the
default account, and it returned 403 on 2026-10-21. If a job fails without an
error or user logs, read the run's RunHistory details (design doc 6).

**Capacity and cost.**

- The cluster has at most three Spot nodes and releases an idle node after 120
  seconds. With both demo profiles on, it is full, and a submitted experiment
  waits in the queue.
- On 2026-10-21, AML did not hand the nodes freed by the demo to the queued
  jobs. It released them and allocated new nodes, which took about ten minutes.
- `aml/first_capacity_wins.sh` queues the same job in several regions and
  keeps the first one that gets its nodes (design doc 7).
- Set `limits.timeout` as a cost cap: 5 hours in `ep-compare-fp8-nd96.yml`, 3
  hours in `ep-compare-nvfp4-nd96.yml`.
- The uksouth Spot meter is USD 12.89 per node-hour (design doc 8.1), so a
  two-node layout costs about USD 26 per hour. The expert-parallel comparison
  used 7.5 node-hours of job time (design doc 8.7). Actual billing was not
  checked.
