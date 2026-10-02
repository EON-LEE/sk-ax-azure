# A.X-K2 on Azure GPUs below H100

Research and reproducible deployment assets for serving **skt/A.X-K2** on
Azure A100 hardware with a standard distributed inference stack.

**Current design:** [docs/SERVING_DESIGN.md](docs/SERVING_DESIGN.md). One vLLM
engine (SKT fork) runs on a Ray cluster of two 8xA100-80GB nodes: tensor
parallel 8 over NVLink inside each node and pipeline parallel 2 across
nodes, behind one OpenAI-compatible endpoint. Capacity comes from the
separate Azure ML low-priority quota (300 vCPU per region). Deployment
assets are in `aml/`.

## Current status

| Evidence | Result | What it does not prove |
| --- | --- | --- |
| **Full 688B on 2 x ND96amsr (16 x A100 80GB), vLLM TP8 x PP2, one endpoint** | **Served and tested**: 6/6 functional, reasoning mode, needle at 1.4K/5.5K tokens, 65-472 output tok/s at concurrency 1-32 | Long-context quality, production SLA, bitwise determinism |
| Real-weight 2-layer cut, vLLM on A100 vs official Transformers FP32 sparse | Pearson >= 0.99995, mean abs log-prob diff ~0.01, top-1 93-100% | Full-depth logit equivalence |
| Dense mode == DSA within `index_topk` (CPU, miniature AXK2) | Passed: prefill and cached decode match; divergence only beyond `index_topk` | Quality beyond 2048 tokens |
| Fork overlay on stock vLLM 0.23.0 | 21 modified base files byte-identical in the released wheel; plus an Ampere MLA dtype fix | Other GPU generations |
| Pinned SKT vLLM sparse kernels on SM80 | Unsupported paths identified | Native DSA on A100 |
| Earlier hand-written block runtime on two A100 VMs | Seven parity cases passed | Serving; superseded by the vLLM design |

All Azure resources from the run were deleted. See
[the handoff](docs/HANDOFF.md) for history and boundaries.

## Repository layout

- `aml/`: Azure ML deployment of the standard stack: region setup, job
  templates, per-node launcher (`src/entry.sh`), fork overlay, dense-mode
  config, client tests, official-reference job, results reader and the
  multi-region capacity watcher. See `docs/SERVING_DESIGN.md`.
- `experiments/`: all authored validation scripts, reference stage code,
  historical Azure runners/provisioning/cleanup scripts, and setup script.
- `evidence/`: frozen selected results and diagnoses, including failures.
  Subscription IDs are redacted. Numerical results and model revisions are retained.
- `docs/HANDOFF.md`: exact scope, unresolved work, cloud safety and migration notes.
- `tests/test_bundle.py`: syntax, evidence consistency and disabled-cloud checks.

Model weights, temporary SSH keys, credentials, raw cloud logs and downloaded
third-party source trees are **not** included.

## Reproduce CPU preflight in a new environment

Use Linux or WSL2, Python 3.12, Git and a CPU build of PyTorch. Native Windows
distributed support differs; the recorded runs used Linux Gloo processes.
Internet access is required to install the pinned dependencies.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-cpu.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python experiments/validate_native_pipeline.py
.venv/bin/python experiments/validate_axk2_request_pipeline.py
.venv/bin/python experiments/diagnose_axk2_chunking.py
```

Alternatively, with an existing `uv` installation:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-cpu.txt
```

The first pipeline test compares serialized and overlapping execution.
The AXK2 test checks request-local caches, padding, generated tokens and CPU
compute overlap with **miniature random weights**, not the real checkpoint.
The diagnostic intentionally records a failed chunked-prefill control; its
successful process exit means the diagnostic ran, **not** that chunking passed.

`experiments/validate_dense_equivalence.py` checks the dense-mode premise on a
miniature AXK2 (CPU only): dense attention equals DSA for every position whose
context fits in `index_topk`, for full prefill and cached decode.

## Run the A100 deployment (Azure ML)

Requires Azure CLI with the `ml` extension, run from Linux/WSL, and explicit
authorization for the spend: two Spot ND96amsr nodes cost roughly USD 17-26
per hour depending on region, and the Spot price cannot be capped.

```bash
export AZ=az SUB=<subscription-id> RG=<resource-group> OWNER_TAG=<you>
bash aml/setup_region.sh swedencentral mlw-axk2-sdc     # repeat per candidate region
python3 aml/render_job.py aml/jobs/rehearse-then-serve-nd96-hub.yml
az ml job create --subscription $SUB -g $RG -w mlw-axk2-sdc \
  -f aml/jobs/.rendered/rehearse-then-serve-nd96-hub.yml --query name -o tsv
bash aml/first_capacity_wins.sh mlw-axk2-sdc=<job> mlw-axk2-wus2=<job> ...
AXK2_MLFLOW_URI=$(az ml workspace show -g $RG -n mlw-axk2-sdc --query mlflow_tracking_uri -o tsv) \
  python3 aml/fetch_results.py show <job>
```

One allocation rehearses the exact launch with a 2-layer real-weight cut
(single-node TP8, then TP8 x PP2 across both nodes) while the full checkpoint
downloads, then serves and tests the full model only if the rehearsal passed.
Delete the resource group afterwards; clusters scale to zero but workspaces,
storage and managed networks remain until deleted.

New outputs are written beside the experiment scripts and ignored by Git.
Committed results stay unchanged in `evidence/`.

## GPU experiments

`setup_axk2_cuda.sh` records the original **guest Linux** setup: PyTorch
2.11.0 CUDA 12.8 and pinned Transformers. It writes under `/opt` and `/tmp`;
do not run it on a shared development host without reviewing those paths.
See `evidence/a100-gpu-package-freeze.json` for the recorded GPU environment.

`axk2_dist_weights.py`, `axk2_dist_prompts.py` and `axk2_dist_pipeline.py` are
the historical **two-block** experiment. They use `/opt/axk2-dist` and the
`RANK` environment variable. They download only the selected checkpoint
tensors, not the full model. The pipeline is an intentionally sequential
correctness baseline, **not a production parallel serving implementation**.

`axk2_request_stage.py` is the newer request-isolated stage used with native
`torch.distributed.pipelining.ScheduleGPipe`. The CPU driver demonstrates
overlapping requests and autoregressive cache handling. Full real-weight
GPU integration remains unfinished.

## Cloud safety

**No script automatically reconnects an account or recreates the old VMs.**
Historical Azure scripts fail immediately unless both
`AXK2_ENABLE_LEGACY_CLOUD=1` and an explicit `AZURE_SUBSCRIPTION_ID` are set.
They discover `az` on PATH or use `AZURE_CLI`.

These scripts retain fixed, expired trial deadlines and historical resource
names/ownership tags. They are an audit trail, **not a portable one-command
deployment**. Review and replace their resource names, networking, deadlines,
cleanup guards and spending limits before any approved new trial. Never merely
remove the deadline guard. The opt-in flag is not spending or deletion approval.
Run Azure orchestration in an explicitly authenticated Linux/WSL environment.

There are no scheduled retries in this repository. Existing app/session state
and authentication do not transfer with Git.

## Pinned upstream sources

- [Model](https://huggingface.co/skt/A.X-K2/tree/2287ca456927eed33b899c404c0b2ffaf17aa09f)
- [Transformers](https://github.com/huggingface/transformers/tree/7fb5bcd1d4b8a5c225a2c33429b2e9e023dd61ae)
- [SKT vLLM](https://github.com/SKT-AI/vllm/tree/023bc76025459d6356c5885f271b9b95c50f93b6)

Original third-party sources and checkpoints retain their upstream licenses.
This repository does not vendor their source trees or model weights.