# A.X-K2 serving design: standard multi-node inference on A100

Snapshot: 2026-10-03. This supersedes the "blocked" conclusion in
[the handoff](HANDOFF.md) for the A100 path. Results of each stage are in
section 8 and in `evidence/`.

## 1. Method review: what was wrong before

**GPU cluster tools do not merge GPUs into one large GPU.** Slurm, Ray,
Kubernetes (KubeRay, LeaderWorkerSet, KAITO), CycleCloud and Azure ML only
*allocate nodes and start processes*. A distributed **inference engine**
(vLLM, SGLang, TensorRT-LLM) creates the "one model, one endpoint" experience:
it shards the weights with tensor/pipeline/expert parallelism, moves
activations with NCCL (NVLink inside a node, InfiniBand/Ethernet between
nodes), and serves one OpenAI-compatible API with continuous batching and a
paged KV cache. Rack-scale systems marketed as "one big GPU" still run model
parallelism underneath.

SKT serves A.X-K2 exactly this way. Their fork contains
`k2_dsa_test/scale_ab/serve_k2.sbatch`: Slurm `srun` with a pyxis/enroot
container image running `vllm serve --tensor-parallel-size 8` on one 8xB300
node. They deliberately chose TP over DP+EP because the DP/EP lock-step hung
in long-context evaluation.

The earlier work in this repository (`experiments/axk2_dist_*`) hand-wrote a
block runtime: FP32 dequantised weights, a custom loader, one blocking NCCL
send/receive per block and no batching, paged KV cache or API. It was a
correctness workaround for the missing SM80 sparse-attention kernels, **not a
serving method**, and it is kept only as a numerical oracle.

Best practice, which this design follows:

- One engine process group behind one endpoint. Common multi-node layout:
  **tensor parallel = GPUs per node** (NVLink), **pipeline parallel = number
  of nodes** (vLLM parallelism docs; KAITO multi-node presets; the Azure
  `inference-toolkit` sample serving Qwen3-235B on 2 x 8 A100 with TP8 x PP2).
- Identical container image and model path on every node; Ray (or Slurm, LWS)
  only forms the group.
- **One region and one network fabric.** Pooling quota from several regions
  into one model is not viable: every decode step would cross a WAN once per
  pipeline stage, and an eviction anywhere stops the whole engine. Separate
  regions can host separate *replicas* behind a load balancer, not one model.

## 2. Capacity without a quota increase

| Pool | Limit in this subscription | A100 80GB nodes it allows |
| --- | --- | --- |
| VM quota, A100 families (all regions checked) | 0 vCPU | 0 |
| VM Spot quota | 100 vCPU per region; increase requests throttled (429) | 1 x ND96amsr (96 vCPU) |
| **Azure ML low-priority compute** | **300 vCPU per region, no per-family cap** | **3 x ND96amsr_A100_v4 (24 GPUs)** |

`Standard_ND96amsr_A100_v4` (8 x A100 80GB, NVLink, InfiniBand) is
low-priority capable for Azure ML in swedencentral, westus2, uksouth,
francecentral, italynorth and polandcentral. Two nodes (192 vCPU, 16 GPUs,
1280 GB) hold the ~656 GiB FP8 weights at ~41 GiB per GPU and leave room for
KV cache. Azure ML retired low-priority VMs on 2026-03-31; such clusters are
now allocated and billed as **Spot**: preemptible, never above pay-as-you-go,
and the price cannot be capped. The Sweden Spot rate was USD 10.55 per node-hour.
See `evidence/azure-capacity-snapshot.json`.

## 3. Architecture

```mermaid
flowchart LR
  client["Client / test suite"] -->|OpenAI API| api["vllm serve (node 0)"]
  subgraph job["Azure ML command job, 2 x ND96amsr_A100_v4, low-priority, one region"]
    subgraph n0["node 0: Ray head"]
      api --> s0["PP stage 0: layers 0-30<br/>TP=8 over NVLink (GPU 0-7)"]
    end
    subgraph n1["node 1: Ray worker"]
      s1["PP stage 1: layers 31-60 + LM head<br/>TP=8 over NVLink (GPU 0-7)"]
    end
    s0 -->|"activations, NCCL"| s1
  end
  blob[("Workspace blob, private endpoint<br/>staged FP8 checkpoint")] -.->|"download at start"| n0
  blob -.-> n1
```

| Layer | PoC choice | Production equivalent |
| --- | --- | --- |
| Capacity | Azure ML compute cluster, low-priority ND96amsr x 2, min 0 nodes | Same SKU on demand/reserved in the customer subscription |
| Orchestration | One AML command job, `distribution: pytorch`, 1 process per node; script starts the Ray head/worker | AKS + KubeRay or LeaderWorkerSet (KAITO), or CycleCloud Slurm |
| Engine | vLLM 0.23.0 + SKT fork, `--tensor-parallel-size 8 --pipeline-parallel-size 2 --distributed-executor-backend ray` | Same command and image |
| Kernels on SM80 | FP8 weight-only Marlin (linear and MoE), Triton MLA decode, FlashAttention-2 MLA prefill | Same; Hopper/Blackwell use native FP8 and DSA kernels |

Files: `aml/src/entry.sh` (per-node launcher), `aml/jobs/*.yml`
(job templates), `aml/render_job.py`, `aml/fetch_results.py`, `aml/Dockerfile`.

## 4. Engine image: stock vLLM plus hash-verified fork sources

The SKT fork `axk2-v0.23.0` @ `023bc760` is upstream `v0.23.0` @ `0fc695fc`
plus 29 commits (47 files). Its single CUDA change, UE8M0 tile-scale rounding
in `cache_kernels.cu`, is gated to SM100 or `VLLM_DS_MLA_UE8M0_SCALE` and
cannot run on A100. All 21 modified Python files are byte-identical in the
released vLLM 0.23.0 wheel and the fork base. Copying the 25 fork Python
files over `vllm/vllm-openai:v0.23.0-cu129` therefore reproduces the fork's
Python tree exactly, with no CUDA rebuild. `aml/src/apply_overlay.py` checks the
installed version, each base-file hash, each downloaded fork-file hash and
the AXK2 registration (`aml/src/overlay_manifest.json`). `aml/Dockerfile`
bakes the same step into an image for production registries. The CUDA 12.9
image variant is used because CUDA 12 minor-version compatibility runs on
older host drivers; the default 0.23.0 image targets CUDA 13.

## 5. Dense mode: running DSA checkpoints without DSA kernels

The fork's DeepSeek-style sparse attention (DSA) indexer and sparse MLA
kernels need SM90/SM100 (FlashMLA sparse, DeepGEMM). The fork also supports
dense mode. `vllm/transformers_utils/configs/axk2.py` attaches the indexer
only when `index_topk`, `index_n_heads` and `index_head_dim` are present, and
the model loader skips indexer tensors otherwise ("so a DSA checkpoint can be
served as a plain AXK2 model"). `aml/src/make_dense_dir.py` symlinks every
checkpoint file and writes a config without those three keys.

DSA lets a query at position *p* attend to the top `index_topk` = 2048 of its
*p + 1* visible keys. While *p + 1* <= 2048, every key is selected, so DSA and
dense causal attention are **the same function**. Beyond 2048 tokens of
context, dense mode attends to more keys than the trained model: an
approximation whose quality must be measured (SKT evaluates with AA-LCR,
RULER and NIAH). DeepSeek uses the same identity in production: "for
short-sequence prefilling, we specially implement a masked MHA mode to
simulate DSA" (DeepSeek-V3.2-Exp report).

## 6. Subscription policy constraints (MCAPS)

A management-group Azure Policy with a `modify` effect forces every storage
account to `publicNetworkAccess=Disabled` and `allowSharedKeyAccess=false`.
The design works within it and does not bypass it:

- Workspace managed VNet (`allow_internet_outbound`) with private endpoints
  to blob, file and Key Vault; system datastores in identity mode; each
  compute cluster has a system-assigned identity with Storage Blob Data
  Contributor on the workspace storage account only.
- The CLI cannot upload a `code:` snapshot from outside the VNet, and Batch
  rejects very long command lines. `aml/render_job.py` puts `aml/src` in the
  `AXK2_SRC_B64` environment variable (base64 tar.gz).
- Artifacts stay in private storage. Jobs report JSON summaries as chunked
  MLflow tags and per-position series as MLflow metrics, read with
  `aml/fetch_results.py` through the workspace API.

## 7. Validation ladder

| Stage | What it proves | Cost |
| --- | --- | --- |
| A. CPU tiny AXK2 (`experiments/validate_dense_equivalence.py`) | Dense == sparse within `index_topk` for prefill and cached decode; divergence beyond it | none |
| B1. 2-layer real-weight cut, official Transformers FP32 sparse on CPU | Reference log-probabilities from SKT's own model definition | CPU minutes |
| B2. Same cut on 2 x NC24ads_A100_v4, vLLM dense, Ray, TP1 x PP2 across nodes vs TP1 single GPU | The exact launch path, overlay and SM80 kernels on real weights, plus multi-node PP parity | ~USD 2 |
| C. Full model on 2 x ND96amsr_A100_v4, TP8 x PP2 | Load and serve all 61 layers; functional, accuracy, determinism, long-context and load tests | ~USD 21/hour |

## 8. Results

Filled in from `evidence/` after each stage; see the README status table.

## 9. Known limits and next steps

- Low-priority/Spot capacity can be preempted; it is for validation, not an SLA.
- Dense mode beyond 2048 context tokens needs a quality evaluation before
  long-context production use. A native SM80 DSA backend (a Triton indexer
  plus sparse MLA over the selected indices) removes the approximation.
- The PoC disables InfiniBand for NCCL (`NCCL_IB_DISABLE=1`); pipeline-parallel
  traffic between the two stages is one hidden-state tensor per step. Enable
  IB in production once the container's RDMA userland is verified.
- Throughput tuning (expert parallelism, more nodes as data-parallel
  replicas, speculative decoding) is out of scope for this proof.
