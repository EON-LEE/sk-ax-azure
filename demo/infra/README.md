# Isolated subscription deployment

This target deployment preserves the complete released FP8/NVFP4 chat, comparison,
MAF, upload, document/table/file tools and eight samples. It does not copy Hosted
Teams identities, app roles, trial settings, source-subscription resources or
credentials. Release `f146262` is the demo baseline: the later successful Hosted
candidate `567f380` changed only the optional Hosted route and JWT dependency.

## Scope and fixed costs

Terraform creates one dedicated resource group, a Linux B1 capacity-one web plan,
Python 3.12 web app, one UK South AML workspace and its Storage/KV/Insights/Log
Analytics dependencies, and one LowPriority `Standard_ND96amsr_A100_v4` cluster.
The cluster has **min=0, max=3**, shared by FP8 TP8/PP2 (two nodes) and NVFP4 TP8/PP1
(one node). Only UK South is configured; there is no automatic regional race.
Both independent profile stores default to **OFF**.

The web identity receives AzureML Data Scientist at the **target workspace only**.
The compute identity receives Storage Blob Data Contributor at its **own storage
account only**. AML uses identity-based system datastore access. Storage and Key
Vault public network access are disabled, matching the destination's enforced
policy. Shared-key authentication and anonymous blob access are also disabled.
No new private endpoints, NAT, Bastion or ACR are deployed. This demo downloads
weights from public Hugging Face and code through its authenticated reverse-link
bootstrap, not from this storage account. Private data-plane access to AML output
blobs from an operator laptop is not established by this deployment; provision an
authorized private access path separately if that becomes necessary.
The existing pinned public vLLM image is reused from Docker Hub; no image build or
weight transfer runs during provisioning. Public image availability/rate limits
and Spot inventory still require a real startup test.

The public Azure Retail API returned Linux B1 in Korea Central at USD 0.018/hour
on 2026-10-19 (about USD 13.14 per 730 hours, before tax/agreements). Storage,
Key Vault operations and telemetry are additional; telemetry has a 0.1 GB/day cap
(not a guarantee of zero charges). GPU min=0 is not sufficient proof of zero:
`status` checks ARM current/target, actual nodes and active jobs. GPU OFF does not
stop web or storage charges. AML Spot quota is separate from ordinary VM quota,
and quota does not guarantee capacity.
For UK South ND96amsr A100 Linux, the same Retail API listed Low Priority at
USD 8.192/node-hour and a separate Spot meter at USD 12.886645/node-hour on
2026-10-19. The three-node configuration therefore represents approximately
USD 24.58-38.66/hour while allocated, pending the actual applicable billing
meter/agreements; this is not a guaranteed invoice rate or a hard budget cap.

## Initialize, provision, deploy

Use WSL/Linux and an **already authenticated target subscription**. These commands
never call `az account set`, switch accounts or run `az login`. Every Azure CLI call
specifies the destination subscription; Terraform and ARM URLs are also explicit.
If necessary, set an existing target-specific `AZURE_CONFIG_DIR` before starting.
Do not copy another tenant's tokens or credentials.

```bash
export PATH="$HOME/.local/bin:$PATH"
D="$HOME/.axk2-targets/<unique-alphanumeric-app-name>"
python3 demo/target.py --directory "$D" init \
  --subscription <destination-subscription-uuid> --name <unique-alphanumeric-app-name>
python3 demo/target.py --directory "$D" provision
python3 demo/target.py --directory "$D" deploy
python3 demo/target.py --directory "$D" status
# Read-only refresh/plan, including on an existing running target (never applies):
python3 demo/target.py --directory "$D" plan
```

`init` saves the explicit tenant/subscription binding using only the named cached
account. It refuses to overwrite an existing binding. The state directory must be
outside the checkout, on a Linux filesystem, mode 0700. All generated files use
0600. Terraform state, input, plan, diagnostic logs, deployment ZIP and generated
admin credentials stay there, not in git or chat. Terraform state can contain
sensitive refreshed app settings: treat **the entire directory as credentials**.
Do not upload it as an artifact or print `terraform show/state` output.
Do not use the legacy `demo/ctl.sh` or `demo/deploy.sh` defaults for this target:
their historical resource/password locations refer to the original demo.
The target runner does not read or overwrite `~/.axk2-demo/passwords`.

The runner takes a nonblocking `flock` for all target operations. Terraform's local
backend separately locks the dedicated state; neither backend nor state is shared
with the source deployment. This is a **single-machine** backend, not a distributed
lock. Never run two copied states concurrently. For team operation, migrate once
to a separately authorized Azure Blob backend with Azure AD and blob-lease locking;
do not reuse an existing subscription's state.

`provision` validates and saves a real plan, rejects deletion/replacement and any
resource ID or resource-group change outside the target, and then applies that
exact saved plan. `safe-plan.json` records resource actions and zero source changes.
FTP and WebDeploy basic authentication are explicitly disabled; the deployment
runner uses an ARM token. Only required target resource providers are registered. Secrets are created by
`deploy`, outside Terraform inputs. No source Web IQ key or Hosted identity is read.

`deploy` uploads once, inspects actual OneDeploy completion, and compares every
source file directly inside persisted `output.tar.zst` with the packaged SHA map.
A timeout is not permission to upload again. Inspect the existing deployment
first. Subsequent redeployments require GPU OFF, avoiding disruption to a live
reverse link. A successful deployment is not proof of model readiness.

## Start/stop without accidental permanent GPU use

```bash
# Read-only: both profiles, actual current/target/nodes and nonterminal AML jobs.
python3 demo/target.py --directory "$D" status

# Stops both profiles durably; waits for TWO zero-node observations.
python3 demo/target.py --directory "$D" off

# ONLY after explicit approval for GPU/download costs. Keep this process attached.
# This example grants a 60-minute lease, INCLUDING startup/download time.
python3 demo/target.py --directory "$D" run --seconds 3600
```

For an explicitly authorized interactive demo that must remain available after
validation, use `python3 demo/target.py --directory "$D" on --keep-on`.
This deliberately has **no automatic shutdown**; the operator must run `off`
after the user finishes. The command refuses an unbounded start without the
explicit opt-in and records the start time in the private target directory.
Do not use it as the provisioning default.

`run` permits 60-7200 seconds,
starts both existing profiles, and invokes durable OFF in `finally`, including
ordinary interrupt/SIGTERM. It never changes compute capacity, creates another
region or launches benchmarks. On controller machine failure/SIGKILL the local
lease cannot run cleanup: invoke the `off` command from the preserved target
directory. Do not use this local lease as a cloud-enforced budget guarantee.
Startup may consume the entire lease without becoming ready; this is a real
failure/not-ready result, not a passing model test.

The released downloader stages a full checkpoint on each node. Public metadata at
the pinned revisions on 2026-10-19 reports approximately 694.386 GB per FP8 node
and 398.278 GB for NVFP4, about **1.787 TB for a cold three-node start**, excluding
images/packages/retries. No existing subscription's weights are copied. Downloads
and model loading are not validated by an OFF-only web rollout.

`off` first uses both authenticated Store/Supervisor power routes, preventing
resubmission, then waits for no bound/active jobs, no nonterminal compute jobs,
ARM current=target=0 and an empty actual node list. Never claim cancellation or
zero cost from a public OFF badge alone. If frontend/auth is unavailable, restore
its saved deployment/credentials before operating; do not only cancel AML jobs
while a live desired-ON supervisor can resubmit them.

## Model-only API preparation and reviewed web deployment

Model-only API credentials are separate from admin/demo credentials and Web IQ.
The API remains disabled when `AXK2_MODEL_API_KEY_SHA256` is unset. Terraform
preserves this externally managed setting rather than accepting its value as a
Terraform variable. Refreshed Terraform state may still contain it; keep the
entire state directory protected as described above.

```bash
# Local preparation only: generates a 256-bit key once; never changes Azure.
python3 demo/target.py --directory "$D" model-key
# Only after the deployment owner receives approval for this exact commit:
python3 demo/target.py --directory "$D" deploy-revision \
  --commit <full-40-character-reviewed-commit> --allow-live-web-recycle
# After disabled-code regression passes, activate in a separate explicit step:
python3 demo/target.py --directory "$D" model-settings --allow-live-web-recycle
```

The raw key exists only in `$D/model-api-backend.key` (0600). The digest is staged
in `$D/model-api-settings.json` (0600), never printed or passed on the command
line. Do not put either value in a document, shell history, evidence, or git.
An existing key is reused. `model-key --rotate` stages a new key and retains the
old digest for overlap; distribute the new key through the approved private
consumer configuration before `model-key --retire-previous` stages removal of
the old digest. A second rotation is refused until the first is retired. These
commands only stage files. After the initial code deployment, activate a staged
rotation/retirement with `model-settings --allow-live-web-recycle`, only in an
explicitly approved window. This settings-only operation requires the saved
successful model API deployment proof and rechecks both original reverse links.

The optional settings use G's model API contract and are also preserved by
Terraform. Defaults need no staging. To stage an override without touching Azure,
repeat `--model-api-setting NAME=VALUE` on `model-key`; existing staged overrides
survive idempotent runs and key rotations. Only the following names are accepted;
invalid options fail explicitly rather than relying on runtime fallback values.

| Suffix after `AXK2_MODEL_API_` | Default |
| --- | --- |
| `NAME_FP8`, `NAME_NVFP4` | `ax-k2-fp8`, `ax-k2-nvfp4` |
| `ALIASES_FP8`, `ALIASES_NVFP4` | `skt/A.X-K2`, `skt/A.X-K2-NVFP4` |
| `MAX_ACTIVE`, `MAX_QUEUE`, `RATE_PER_MINUTE` | `4`, `8`, `60` |
| `QUEUE_TIMEOUT`, `TIMEOUT` (seconds) | `30`, `300` |
| `MAX_TOKENS`, `DEFAULT_MAX_TOKENS` | `8192`, `4096` |
| `MAX_BODY_BYTES`, `MAX_MESSAGES` | `2097152`, `400` |
| `MAX_TOOLS`, `MAX_SCHEMA_BYTES` | `64`, `131072` |

`deploy-revision` packages the exact git commit, not the current working tree,
and refuses short SHAs, tags, branches, unsafe archive paths and symlinks. Fetch
and review the candidate before using its SHA. It verifies the saved rollback ZIP
against its manifest and the actual deployed archive, then retains that ZIP,
manifest, app settings and current profile bindings in a new private rollback
directory. Existing settings are preserved; only the staged API setting changes.
It does not call GPU power, change compute, submit jobs or apply Terraform.

Unlike the ordinary OFF-only `deploy`, this operation explicitly permits a live
web recycle. App Service ZIP deployment and app-setting updates are **separate
operations**. The approved procedure permits **two web recycles, zero GPU
restarts**, in one coordinated window. The API code deploys first with no digest
configured, so the new routes are briefly disabled (404). Before activation,
check health, status, capabilities and disabled routes, and require both existing
model reverse links to recover with unchanged profile activation times and empty
queues. Only then run `model-settings` to activate the credential, wait for both links
and empty queues again, and run smoke. `deploy-revision` never activates the key.
These health checks are not inference tests.
After either recycle, the runner enforces a minimum four-minute settling period
and three consecutive complete checks at least 15 seconds apart, bounded to ten
minutes. Each check includes both unchanged healthy profile bindings, empty
queues, health/status, Web IQ discovery and the expected unauthenticated model
API status (404 before activation, 401 afterward). An old worker responding
before the settings recycle is not evidence that the new worker has recovered.

After deployment, run the **reviewed candidate's**
`demo/tools/model_api_smoke.py` using its protected key-file argument, not a key
value in argv. Export that reviewed file from the same exact deployed commit to
the private target directory; do not execute a different branch's current copy.
With `$URL` set to the target's public URL, the approved bounded NVFP4 sequence is:

```bash
python3 "$D/model_api_smoke.py" \
  --base-url "$URL/models/nvfp4/v1" --model ax-k2-nvfp4 \
  --key-file "$D/model-api-backend.key" \
  --only models,chat,stream,tool,tool_result,auth \
  --max-tokens 64 --timeout 120 --out "$D/model-api-smoke.json"
```

This is at most four model admissions (chat, stream, tool call, tool-result
replay), concurrency one, thinking OFF, with a 64-token cap per admission.
Model listing and missing/wrong-key rejection consume no model admission.
The tool emits secret-free per-check outcomes, status codes, token usage,
TTFT/TPOT and elapsed time; inspect actual results rather than assuming success.
Do not run a second smoke attempt automatically.
`candidate-proof.json` explicitly records smoke as not run until this separate
verification completes.

If upload transport or OneDeploy is uncertain, inspect that deployment instead
of resubmitting. If recovery fails, preserve the candidate/rollback evidence and
report the blocker. The private rollback directory contains the previous ZIP and
settings for a separately approved recovery; do not shut down or restart GPUs as
a frontend rollback strategy.

## Web IQ and optional services

Web IQ is explicitly unavailable until **authorized target-specific** credentials
are configured. Its unavailable reason appears in `/api/capabilities` and the
existing tool/action UI. Do not substitute an unrelated search/model provider.

```bash
# Inject these through your secure shell/secret manager; do not put real values
# in command history, tfvars, source code, Terraform output or a chat.
export AXK2_WEBIQ_ENDPOINT
export AXK2_WEBIQ_API_KEY
python3 demo/target.py --directory "$D" webiq
unset AXK2_WEBIQ_API_KEY
```

The command requires both values, writes only a temporary 0600 file and sends it
to **this target app**. It does not extract or transfer another tenant's key.
Recheck `/api/capabilities` to perform discovery (no search/inference). An allowed
endpoint and credential do not prove a successful search or acceptable query use;
those need their own authorized test. OCR and external code sandbox remain
`not_configured`, as in the source demo; uploaded/model code is never run on the
web host or GPU as a substitute.

## Recovery and validation

Keep an encrypted, access-controlled backup of the private target directory after
apply and deployment. If the machine is lost, recover the same state/config and
credentials before operating. Never `init` a new state over existing resources,
force-unlock an active owner, import source resources, or delete resources to
work around a stale lock.

For failed OneDeploy, inspect `https://<app>.scm.azurewebsites.net/api/deployments`
with target ARM authentication. Wait for terminal state before a retry. Preserve
the last-good ZIP and expected map. Check Oryx manifest and compressed `antenv`
dependencies as well as source hashes: intact source without uvicorn cannot boot.
Do not restart healthy GPU jobs to repair a frontend package.

```bash
terraform -chdir=demo/infra fmt -check
terraform -chdir=demo/infra init -backend=false
terraform -chdir=demo/infra validate
# In a separate WSL Python 3.12 environment with frontend requirements + aiohttp:
python -m unittest discover -s tests -p 'test_demo*.py'
```

Post-rollout checks: root/static UI, eight samples and selector/comparison behavior;
`/healthz`; `/api/models` with both profiles OFF; `/api/capabilities` with truthful
Web IQ/sandbox/OCR status; attachment create/upload/read/close; persisted archive
hashes; and `status` effective_zero. Report GPU generation, tool-driven model
behavior and comparative performance as **not tested** unless actually exercised.
