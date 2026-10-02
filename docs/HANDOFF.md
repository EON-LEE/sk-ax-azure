# Development environment handoff

Snapshot: 2026-10-02. Read this before allocating any GPU.

## 2026-10-03 update: standard-stack redesign

The method changed. See [SERVING_DESIGN.md](SERVING_DESIGN.md). The
hand-written block runtime below is superseded as a serving approach and kept
only as a numerical oracle. The target is SKT's own practice, `vllm serve` on
the SKT fork, scaled out the standard way: Ray cluster, TP8 inside each
8xA100 node, PP2 across two nodes, one endpoint. Capacity comes from the
separate Azure ML low-priority quota of 300 vCPU per region, not VM quota.

Result: the full model was served on 2 x ND96amsr_A100_v4 in italynorth
(TP8 x PP2, one endpoint) and passed the functional, reasoning, needle and
load tests. A 2-layer real-weight cut matched the official Transformers
implementation. See SERVING_DESIGN.md section 8 and `evidence/a100-*.json`.

Azure state: **everything was deleted** after the run (resource group
`rg-axk2-aml-208d24c1`: 12 policy-compliant workspaces, storage including
the 694 GB staged copy, Key Vaults, private endpoints, clusters). No
paid resource or queued job remains. To repeat, follow the README section
"Run the A100 deployment". Expect Spot capacity hunting: on 2026-10-02,
2 x ND96amsr could not be allocated in swedencentral, westus2,
francecentral or polandcentral; italynorth and uksouth allocated.

Fixes the run required, all now in `aml/`:

- Storage policy forces private, key-less storage. Use a managed VNet,
  identity datastores, compute identities, an env-var script payload and
  MLflow-tag results.
- vLLM 0.23 images do not ship Ray; install `ray[cgraph]` first.
- Ampere FP8 Marlin bug in MLA chunked-context prefill (int32 cast), patched
  by `apply_overlay.py`.

## Goal and non-negotiable distinction

Run the **entire real A.X-K2 checkpoint**, not a smaller replacement, below H100.
The customer needs parallel inference across available GPUs, including a
possible heterogeneous T4/A100 fleet. Merely passing hidden activations through
VMs one after another is not proof of useful concurrent serving.

Required final evidence:

1. All 61 pretrained blocks, embeddings, final normalization and LM head load.
2. Actual generated responses and explicit request/cache correctness checks.
3. GPU execution traces demonstrating overlapping computation on different GPUs.
4. First-token/inter-token latency, throughput, GPU memory and transfer stalls.
5. All-or-nothing minimum-cluster acquisition with bounded time and cleanup.

As of 2026-10-03 the vLLM deployment meets items 1, 2, 4 and 5. Item 3 is
implied by concurrent throughput across the two pipeline stages, but no
explicit kernel-overlap trace was captured. Transfer stalls were not measured.

## What was verified

### Single A100

Pinned SKT vLLM sparse paths reject SM80. Sparse MLA backend guards and the
DeepGEMM indexer are independent blockers; removing the guards is not a fix.
See `evidence/axk2-source-results.json`.

A custom correctness-first Transformers path ran on an actual A100 80GB:
FP32 compute with lower-precision weight storage and on-demand projection
expansion. Selected full-width/random two-block cases and a genuine checkpoint
expert passed. Strict failures, including an earlier random-model 8K case, are
retained. See `axk2-a100-resolution.json` and the `axk2_*_trials-results.json`
files in `evidence/`.

### Two actual checkpoint blocks across two VMs

Block 0 ran on rank 0; block 1, including all 256 experts, ran on rank 1.
Rank 1 held an extra copy of block 0 **only for the serial reference**.
Seven 256/4K/8K random/bilingual cases had zero distributed-versus-serial
prefill/decode error. Full-versus-cache controls passed in those cases.

This driver waits for an acknowledgment after each activation transfer.
It is **sequential sharded execution**, not overlapping pipeline serving,
tensor parallelism or expert parallelism. It used NCCL/TCP, not RDMA.
Its reference shares the custom loader/operators; it does not independently
prove equivalence to the official H100 engine.

Necessary real-checkpoint loader fixes:

- Handle partial edge blocks in 128x128 FP8 scaling. The original
  `kv_a_proj_with_mqa` matrix is 576x7168 with scale grid 5x56.
- Load the original `e_score_correction_bias` buffer, not initialized zeros.
- Check tensor hashes/shapes, complete consumption and no remaining meta tensors.

See `evidence/axk2-distributed-resolution.json` and `dist-rank*-result.json`.
Historical script hashes refer to the original execution artifacts before
repository packaging/line-ending normalization, not necessarily the current
source file bytes.

### Native overlapping scheduler on CPU

`validate_native_pipeline.py`: four local processes, eight microbatches, native
PyTorch `ScheduleGPipe` with `loss_fn=None`. Serialized control observed one
active stage; pipelined control observed four. Output parity passed.

`validate_axk2_request_pipeline.py`: miniature random **61-layer AXK2**,
eight variable-length requests, separate request caches, final norm/head and
four greedy output tokens. Four CPU stages overlapped and generated token IDs
matched the official serial CPU model. Stale cache metadata is rejected.

These are CPU process compute spans. On CUDA, the stage's wall-clock spans
are **launch intervals only**, not proof of overlapping GPU kernels.
The fixed request cohort is not a production continuous-batching scheduler.
EOS handling, cancellation, request admission and failure recovery remain work.

### Chunked-prefill failure

`diagnose_axk2_chunking.py` reproduces a discrepancy **inside the unmodified
official miniature CPU model** between whole-prefill and chunked-prefill paths:
maximum error about 0.1307. The custom stage matches the official path when
given identical chunks. Root cause is not established.

Do not relax tolerance or enable chunked prefill by default to make memory fit.
The passing pipeline test uses whole prompts with valid lengths; fixed
transport padding is excluded from cache updates.

## Azure state and authorization boundaries

- All paid single-/two-A100 trial VMs and dedicated groups were deleted.
- No full-model GPU cluster was created; no inference endpoint is running.
- No app automation remains active for retrying quota.
- The source subscription was classified `Internal_2014-09-01`; this is not
  evidence that internal status caused a specific failure.
- Seven candidate regions had regional Spot quota 100 vCPU and regular
  A100/T4 family quota zero where checked.
- Sweden `ND96amsr_A100_v4` appeared unrestricted in the SKU API, but physical
  capacity and Spot allocation were never verified.
- Two requests for Sweden Spot 192 vCPU returned `RequestThrottled` (429),
  including the retry after the required 3600-second wait.
- No accepted request ID or approval was confirmed. Support escalation
  evidence was prepared, **not submitted as a support ticket**.

See `quota-support-escalation.json` and `regional-quota-diagnosis.json`.
Subscription identifiers in committed evidence are replaced with the all-zero
UUID. Use the intended authenticated subscription explicitly; do not infer or
switch accounts from an environment's defaults.

The user approved, in the original session, a bounded Sweden trial of two
8-GPU A100 Spot VMs, four hours from first allocation, at most USD12/VM/hour
and USD96 compute plus disk/network. This is **historical scope**, not an
unlimited or automatically transferable deployment authorization. The later
T4/A100 mixed-fleet discussion requested investigation, not fleet provisioning.

## No-quota-increase alternatives under review

The 100 Spot vCPU pool is shared across GPU types within a region, not 100
per GPU family. Do not sum regional pools into a cross-region inference cluster.

| Configuration | vCPUs | Nominal GPU memory | System RAM |
| --- | ---: | ---: | ---: |
| 25 x NC4as_T4_v3 | 100 | 400 GB | 700 GiB |
| 19 x T4 + 1 x NC24ads_A100_v4 | 100 | 384 GB | 752 GiB |
| 13 x T4 + 2 x NC24ads_A100_v4 | 100 | 368 GB | 804 GiB |
| 4 x NC24ads_A100_v4 | 96 | 320 GB | 880 GiB |
| 1 x ND96amsr_A100_v4 | 96 | 640 GB | 1800 GiB |

Original FP8 weights are approximately 656 GiB, excluding caches/workspace.
None of these nominal GPU totals proves the model fits. Host memory is not
automatically pooled with GPU memory.

Potential paths, all unverified end-to-end:

- A100 multi-VM pipeline with host-resident FP8 weights and bounded GPU weight
  caching/prefetch, using genuine request overlap.
- One NVLink-connected eight-A100 node with host offload; easier topology,
  but **not** multi-VM evidence.
- Heterogeneous T4 experts/A100 other work with load-aware expert placement,
  communication/combine scheduling and host offload. No such engine exists in
  this repository yet. T4 SM75 lacks native BF16/FP8 arithmetic, so A100
  compatibility evidence is not transferable.
- Additional 4-bit quantization is a separate accuracy/kernel project,
  not a lossless storage change or a known-supported A.X-K2 configuration.

Acquire a complete minimum viable fleet within an explicit deadline; delete
partial allocations if the fleet cannot be formed. Do not hold many T4 VMs
indefinitely while waiting for A100 capacity.

## Work remaining before paid validation

1. Select a feasible topology and runtime based on existing quota, interconnect
   and measured memory, not only summed GPU capacity.
2. Implement full-checkpoint partition/download/loading and bounded host/GPU
   storage without expanding all expert weights simultaneously.
3. Integrate real-weight execution with request-local KV state and the native
   scheduler, or a verified alternative supporting the chosen parallelism.
4. Complete EOS, admission, transport failure and profiling behavior.
5. Run local correctness and error-path checks; then obtain a bounded deployment
   scope and implement current provisioning/cleanup guards.
6. Validate real GPU overlap, responses, latency and throughput. Preserve
   failures; do not report a smaller-model or partial-block pass as completion.

## Historical tooling

All authored scripts are in `experiments/`. Cloud runners are opt-in disabled
via `cloud_context.py`. Provisioning scripts retain expired October 2, 2026
cutoffs; keep these until a reviewed replacement supplies fresh time/cost guards.
Remote guests expect Linux `/opt` and `/tmp`; these paths are not host-user paths.

Source snapshots can be fetched again with
`python experiments/validate_axk2.py --help` to inspect supported modes;
the tool uses pinned public revisions. Do not download full checkpoints merely
to reproduce CPU tests. Several historical GPU trials write results to `/tmp`;
inspect each script before running it.

Original raw session logs, private keys, account caches and downloaded weights
were intentionally excluded. Selected numerical failure evidence is retained.
A successful Git transfer does not transfer Azure authentication, quota,
running resources, app automations or guarantees of GPU availability.
