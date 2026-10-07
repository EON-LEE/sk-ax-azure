# Development environment handoff

Snapshot: 2026-10-06. Read this before allocating any GPU.

## 2026-10-07: AX Microsoft Agent Framework demo

The frontend now uses Microsoft Agent Framework Python 1.20.0 `Agent`,
function invocation layer and function middleware over the existing reverse
link. A.X K2 alone performs inference, including thinking/tool decisions/code.
The minimal chat renders actual pending/running/success/error events, files,
diffs and on-demand sanitized HTML/SVG artifacts. See `demo/README.md` for
capability isolation, expiry, upload limits and exact supported tools.

The desktop follow-up uses a shared capped 1200px chat/composer width, 18px
body text and 42px principal controls. Native PDF reading now uses explicit
page chunks/citations and typed outcomes; newly attached PDFs must actually
be parsed before any classification. The custom relay now transports MAF's
`options.instructions` (previously omitted), including tool guidance and
the exact workspace inventory. Do not infer that a private PDF is scanned
from its filename or size; OCR remains unavailable.

Same-token native conversation/tool replay is RAM-only and bounded; no long-term
memory is implemented. New chat cancels the actual task and revokes its workspace.
Per-turn tools-off and clear direct search/tool prohibitions are enforced at registration
and invocation. Execution requests use bounded A.X continuation rather than fictitious
progress. New authorized PDFs use actual MAF `read_pdf`/middleware first-page preflight,
including real failure cards; failed/cancelled reads are not marked checked. This fixes
the former mandatory-PDF guard that could terminate on a model's wrong/missing call,
not a demonstrated parser failure in the user's private document. More pages still
require actual reads. Terminal state is event-driven; no work continues after stream end.

Web IQ has a genuine standard MCP adapter, with input-schema discovery and
direct `x-apikey` authentication verified against existing authorized
`EON-LEE/tmap-webiq-poc` prior art. It does not invoke Foundry inference.
General public queries are supported; uploaded content and filenames cannot be
automatically sent externally. Securely configure the existing approved enterprise
key/access (see `demo/README.md`); do not create a connection or grant new
permissions. `/api/capabilities` reports the actual state and whether a real
query succeeded. No operational search tool is registered before discovery.
Protocol-fixture tests alone do not establish live enterprise access.

Web IQ activation was verified on 2026-10-07 using the user's separate
AX-only key input (`.env.webiq`, git-excluded and never bundled) and an
approved memory-only transfer to secure App Service settings. Real MCP
initialize/discovery and bounded passage queries return Microsoft Learn
sources. The existing A.X job executed `web_iq_search` through real MAF,
streamed thinking and synthesized an answer citing actual returned URLs;
`/api/capabilities` reported `ready` and `verified_search: true`.
An independent browser showed the real search card and returned source
links. The subsequent public-search extension removes the original five-topic
cap and registers all ten actually advertised MCP verticals as native MAF tools:
web/news/finance/places/autosuggest/browse/images/videos/sports/sonic.
Each tool has its own discovered schema and bounded options. Public provider
smoke calls returned all ten responses; Samsung finance returned symbol 005930,
KRW and LSEG with a trade timestamp, but no exchange/delay fields. Autosuggest
returned no suggestion list; sports returned games without citation URLs.
No invented sources, live-feed claims or media embedding rights. Attachments
require explicit current-turn public search terms; upload-derived queries are
blocked. Browse is indexed-only with public-domain/DNS/credential URL checks,
no live crawl/dynamic rendering. OCR and isolated code execution remain
unavailable. No GPU changes.
Tests require an approved external isolated sandbox; no uploaded/generated
code is executed on App Service or GPU, and no test pass is invented.
Do not provision resources or grant permissions without user approval.

**GPU stays on until the user explicitly requests otherwise.** Frontend-only
deploy uses `AXK2_FRONTEND_ONLY=1` and must not change jobs/weights/cluster
settings or start benchmarks. Older shutdown advice below is superseded.
Existing benchmark samples remain partial and cannot establish equivalence.

Verified on the live existing A.X job: exact `2^64-1` and Seoul time;
README/HTML reads, HTML write + unified diff + sanitized preview + download;
CSV sum + SVG chart; genuine error results for unconfigured services in the original release.
Thinking-enabled file work preserved the real `reasoning_content` stream.
Independent browser tab verified three model rounds, cards, on-demand preview,
attachment read and original 390px mobile layout (no permanent editor/admin chrome).
The follow-up requirement is desktop only, not additional mobile work.
These are functional demo checks, not new model-quality benchmarks.

## 2026-10-06: customer demo and model-card benchmarks

**Demo.** `demo/` runs an always-on chat page on App Service (Korea Central, B1),
backed by the 2 x ND96amsr_A100_v4 serving job (FP8, native DSA, TP8 x PP2).
`demo/README.md` covers deploy and operations; `REPORT.md` section 9 has screenshots.

- **UI:** one ChatGPT-style chat page ("A.X K2 고객 데모"). It shows thinking as a
  collapsible section and tool calls as cards. It has no cluster or results views;
  those numbers live only in `docs/report`.
- **Open demo:** `AXK2_OPEN_DEMO=1` is the `deploy.sh` default, so anyone with the
  URL can chat without logging in, as requested for an internal demo. Redeploy with
  `AXK2_OPEN_DEMO=0` to require the demo password. `/admin` always needs the admin
  password. Passwords live in `~/.axk2-demo/passwords` (WSL), never in the repo.
- **GPU:** `demo/ctl.sh on|off|status`. `on` races low-priority jobs in uksouth,
  italynorth and francecentral, keeps the first with 2 nodes, and is ready in
  about 25 min. 2 low-priority nodes cost about USD 16/hr. **Only run `ctl.sh off`
  on an explicit user request.**
- **Measured in the demo:** first token 0.4 s, 45-49 tok/s per user.
- **Deploy gotcha:** `deploy.sh` can end with "Kudu Status 502" while Oryx is still
  building. If the site then crashes with `No module named uvicorn`, the build was
  cut off; rerun `deploy.sh`.

**Benchmarks** (`evidence/a100-benchmarks.json`, run `ev20261006-095528`). These use
the model card's thinking-mode settings (temperature 0.6, top_p 0.95) with public
data and scoring. Each suite ran on a random sample. The run was stopped at about
55% by request, so the scores are indicative:

| Suite | SKT | A100 x16 | done/planned | 95% CI |
|---|---:|---:|---:|---:|
| AIME26 | 97.1 | 100.0 | 10/60 generations | small n |
| KoBALT | 73.0 | 74.3 | 109/200 | ±8.2 |
| CLIcK | 91.6 | 85.5 | 131/200 | ±6.1 |
| IFBench | 75.9 | 78.6 | 112/200 | ±7.6 |
| NIAH | 100 | 100 (9/9, 32K-256K) | verification run | - |

`python docs/report/make_figures.py --only eval` rebuilds the chart and the
HF-card table (`docs/report/figures/benchmark-table.md`).

Still open:

- Finish or rerun the eval for full n, mainly to settle CLIcK.
- Make the PPTX of the report. The Office MCP tool needs a Microsoft 365 account
  connected; it was not connected on 2026-10-06.
- Run quality checks on customer data.
## 2026-10-04: SKT's NVFP4 checkpoint on one A100 node

The 2026-10-03 entry below said NVFP4 cannot run on A100. That was wrong:
the required compute capability 8.9 is a software check in vLLM 0.23, which
upstream vLLM lowered to 8.0 in v0.24.0. With that change backported
(`aml/src/nvfp4_sm80_port.json`, opt-in `NVFP4_SM80_PORT=1`), SKT's
`skt/A.X-K2-NVFP4` was served on **one** ND96amsr (8 x A100, TP8, native
DSA) in uksouth, job `frank_nail_jp9txc21y1`. SERVING_DESIGN.md section 8.4
has the results; the evidence is `evidence/a100-nvfp4-single-node.json`.

- **Checks:** functional 4/4, tool calls 9/9, SKT's needle test 9/9 up to
  244,898 tokens. Weights take 48.5 GiB per GPU. The KV cache holds 254,016
  tokens, or 272,960 with 2,048 batched tokens, which fits the full
  262,144-token context.
- **Kernels:** the NVFP4 experts run W4A16 through Marlin, because A100 has
  no FP4 tensor cores (B200 runs them W4A4). FP8 layers and attention run as
  before.
- **Speed:** the same output tok/s as the FP8 checkpoint on 16 A100 with 1K
  inputs up to concurrency 32 (1.01-1.06x), 0.81-0.86x at 64-128, and
  0.64-0.98x with 8K-32K inputs, where prefill is slower. Under the tech report's Fig. 7
  conditions it reaches 0.47-0.49x of one B200 node at 1K-4K inputs, about
  half of B200 per GPU instead of a quarter, and 0.20-0.35x at 8K-32K.
- **Cost:** 38-56% less per million output tokens than FP8 on two nodes.
- **Limits:** it relies on the backport, so use vLLM 0.24 or later in
  production. Quality was checked only with the probes above. The KV cache
  holds 38-41% of the two FP8 nodes' 673,792 tokens.

Azure state: everything was deleted again (resource group
`rg-axk2-nvfp4-208d24c1`, 15:51Z). Workspace names with `-r4-` are now
soft-deleted for 14 days. The run used at most about 2.4 node-hours, about
USD 20-31.

## 2026-10-03 (third update): verification run and the tech report's speed

Everything left open was tested in one allocation of the same 2 x ND96amsr
(uksouth, job `wheat_yak_fq7rn5533b`). SERVING_DESIGN.md section 8.1 has the
results; the evidence is `evidence/a100-verification-and-doc-speed.json`.

- **InfiniBand:** works inside the Azure ML containers with GPUDirect RDMA,
  at 20.6 GB/s per GPU pair versus 0.35 GB/s over TCP. All multi-node
  templates now probe it (`IB_PROBE=1`) and use it when NCCL confirms it.
- **Tool calling:** 9/9 in both modes. It needs `--enable-auto-tool-choice`,
  which the model card command lacks; without it `tool_choice: auto` returns
  raw `<tool_call>` text.
- **Long context:** SKT's needle test passed 9/9 in native mode at 32K, 128K
  and 256K (up to 252,728 tokens). Dense mode passed 3/3 at 32K and failed
  all six at 128K and 256K, so it is limited to about 60K tokens.
- **Determinism:** default kernels diverge after 68-264 characters.
  Batch-invariant mode cannot start on A100: no FP8 MoE kernel for SM80
  supports it.
- **Speed versus tech report Fig. 7** (one B200 node, concurrency 32,
  1K output): native on 16 A100s reaches 0.43-0.51x at 1K-8K inputs and
  0.10-0.36x at 16K-120K, where the KV cache (673,792 tokens) limits
  concurrency. Dense reaches 0.52-0.64x and 0.23-0.58x on the same ranges.
- **EAGLE3:** not usable. It cannot run with PP. On TP16 the A100 MLA decode
  kernel cannot verify multi-token drafts: start-up with CUDA graphs fails,
  and eager mode is 4-5x slower.
- **NVFP4:** reported as not possible on A100 (needs compute capability 8.9).
  **Corrected on 2026-10-04:** that is a software check, and the checkpoint
  runs on one A100 node (see the entry above).

Azure state: everything was deleted again (resource group
`rg-axk2-aml-208d24c1`). Workspace names with `-r3-` are now soft-deleted for
14 days. The run cost about USD 80-125.

## 2026-10-03 (second update): native DSA on A100

The model now runs on A100 with its published weights and config and its own
DeepSeek Sparse Attention rule. The attention is computed by substitute Triton
kernels: a hash-checked port of upstream vLLM PR #38476 (`TRITON_MLA_SPARSE`
plus Triton indexer logits), described in SERVING_DESIGN.md section 5b and
`aml/src/dsa_port.json`. Section 5c lists every difference from the reference
single-node deployment, including the gaps: tool-call parser not set and
contexts beyond 60K untested.

- **Kernel tests:** 94/94 pass on A100.
- **2-layer real-weight cut:** native stays at the BF16-versus-FP32 floor
  beyond `index_topk` against the official FP32 sparse model, while dense mode
  drifts.
- **Full 688B model:** served natively and densely in one allocation on
  2 x ND96amsr (TP8 x PP2) and measured. Both modes passed 6/6 functional
  checks and found the needles 12/12 up to 60K tokens. Native gave 50-812
  output tok/s at concurrency 1-128 with TPOT 19-21 ms from 1K to 32K context;
  dense was 10-60% faster but exact only up to 2,048 tokens. Details are in
  section 8.2 and `evidence/a100-native-*.json`.

Azure state: everything was deleted again (resource group
`rg-axk2-aml-208d24c1`, 02:09Z). The deleted workspace names remain reserved
for 14 days, so reuse needs new names. The first native run's Hub download
stalled at 68%; `stage_weights.py` now has a restart watchdog. The round cost
about USD 71, as recorded in `evidence/a100-native-dsa-deployment-log.json`.

Still open: a full quality evaluation (AA-LCR/RULER or customer data);
upstream fix #49139 for batches of more than one sequence longer than 32K
tokens; InfiniBand for NCCL; bitwise reproducibility; and on-demand capacity
in the customer subscription.

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

As of 2026-10-03 the vLLM deployment meets items 1, 2, 4 and 5, in both
native DSA and dense mode. Item 3 is implied by concurrent throughput across
the two pipeline stages, but no explicit kernel-overlap trace was captured.
Transfer stalls were not measured.

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
  SKT has since published `skt/A.X-K2-NVFP4`; SERVING_DESIGN.md section 8.4
  serves it on one A100 node.

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
