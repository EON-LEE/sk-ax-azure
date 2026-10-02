# A.X-K2 on Azure GPUs below H100

Research and reproducible experiments for serving **skt/A.X-K2** on Azure
A100/T4 hardware. This is **not a completed serving engine or deployment**.

## Current status

| Evidence | Result | What it does not prove |
| --- | --- | --- |
| Pinned SKT vLLM sparse kernels on SM80 | Unsupported paths identified | A100 support in unmodified SKT vLLM |
| Actual A100 operator and real-expert tests | Passed selected cases; failures retained | Full-model correctness or performance |
| Real checkpoint blocks 0 and 1 on two A100 VMs | Seven cases passed distributed-versus-serial parity | Concurrent pipelining: this experiment was sequential |
| Native PyTorch pipeline, miniature 61-layer AXK2 on four CPU processes | Eight requests overlapped; greedy tokens matched the official serial CPU model | Real 688B weights or GPU overlap |
| Chunked prefill on the miniature official CPU model | Failed a full-versus-chunked control | Safe chunked prefill for the full model |
| Full 688B multi-GPU generation | **Not run** | Customer-ready serving |

**All earlier paid GPU trial resources were deleted.** The proposed full-model
trial was blocked by repeated Azure quota throttling. No service is running.
Read [the handoff](docs/HANDOFF.md) before continuing.

## Repository layout

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