# A.X hosted-agent local contract experiment

This is **not a cloud deployment**. It exercises the real Microsoft Agent
Framework and Foundry `ResponsesHostServer` against the existing A.X text-chat
wire contract. Tests supply a deterministic transport, not A.X inference.
The adapter rejects non-loopback endpoints until an authenticated gateway is
approved. No GPT client, fallback, demo credentials, deployment command, or Teams
publication is included.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --pre -r experiments\hosted_ax\requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_hosted_ax.py
```

Run both Hosted and unregistered MCP contracts with
`.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_hosted_ax*.py"`.

The entry point accepts `AX_CHAT_ENDPOINT` ending in `/api/chat` (FP8) or
`/models/nvfp4/api/chat` (NVFP4), **on loopback only**. It is an experiment, not an
OpenAI-compatible model URL. Production `/api/agent` remains an App Service MAF
agent, and proxying it would not demonstrate Hosted Agent execution.

## Approved-stage boundary

Target path: Teams -> Foundry Activity bridge -> Responses -> Hosted MAF
orchestration -> authenticated model gateway -> existing A.X reverse relay.
The gateway must forward model inference only; it must not invoke `/api/agent`.
Its authorization must validate the agent identity and intended audience with
least privilege. Demo cookies/passwords are not the production auth design.
GPU ports remain private. Existing GPU jobs, compute and App Service are unchanged.
The existing relay has no reviewed agent-identity model endpoint. A separate
model-only route wired to `ChatRelay` may be needed; that is an operating-owner
change requiring approval, not something an HTTP adapter alone can supply.

The current `/api/chat` allow-list fixes tool schemas rather than forwarding
arbitrary MAF tools. Therefore this experiment supports text only and rejects
tool/file input explicitly. Tools must execute inside Hosted MAF after a reviewed
model-only gateway supports the needed tool-call contract. Teams attachments,
PDF handling, user delegation and end-to-end cancellation remain unverified.

`ResponsesHostServer` uses AgentServer history and removes downstream storage
options for this non-storing client. That does **not** disable Foundry or Teams
retention. Do not add cross-conversation memory. Foundry persists conversation
history separately from per-session sandbox state; documented sandbox deletion
after 30 days of inactivity is not a conversation deletion guarantee. Review
reset/deletion APIs, tenant retention and telemetry before using company data.
Local SDK state must use a temporary `AGENTSERVER_STATE_ROOT` in tests.
These local history tests are not a tenant authorization or cross-user security
test. Local Responses API cancellation closes the fixture model stream; actual
Teams cancellation and GPU abort propagation still need end-to-end evidence.

## Official evidence checked 2026-10-08

- [Hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents):
  BYO code, downstream services, Responses-to-Activity Teams bridge, isolated
  sessions; Korea Central is listed. Hosted means managed execution, not no-code.
- [New agent model](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/migrate-agent-applications):
  use a unique identity and stable endpoint, not legacy Agent Applications.
- [Teams publishing](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot)
  and [REST publishing](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network)
  (2026-10-06): current documentation explicitly supports REST publishing.
  This contradicts the migration article's portal-only statement; prefer the
  newer, purpose-specific publishing contract, pending live API verification.
- [Deployment](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent):
  port 8088, Linux amd64 images, dedicated agent identity, billed CPU/memory.
- [Source ZIP deployment](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code):
  optional ACR-free path, Python 3.13/3.14 or .NET 10; **not tested here**.
- [Publishing troubleshooting](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/troubleshoot-publish-copilot):
  Copilot Chat does not require a Copilot license for basic agent use, but tools
  and tenant-data access can have licensing/usage requirements. Company Teams
  availability, app policies and entitlement still require tenant verification.

No evidence establishes native prompt-agent registration of this private A.X
model. The supported path under investigation is BYO-code Hosted Agent calling
the model through a secure downstream gateway.

## Separate approvals required

Confirm the Foundry/Teams tenant relationship and resource-owner permission
before reusing any project or registry. Approve a bounded hosting cost/time limit,
new agent identity/version and any required Foundry/RBAC connections. For the
container path, approve image upload and scoped ACR pull/push access; a new ACR is
not inherently required. Source ZIP is an alternative requiring its own upload
approval and supported runtime.

Approve the model gateway location, authentication/network changes and trace
policy separately with the operating owner. Approve Bot Service provider
registration/resource/channel creation and Teams publication/installation/test
messages separately. Start with individual scope, not organization-wide access.
The documented individual publishing flow needs no organization-wide approval,
but still requires Azure permissions and compatible tenant policy; tenant scope
requires Microsoft 365 admin approval.

Only a real company Teams message correlated with Hosted execution and an A.X
inference trace will satisfy the final goal. Local fixture success does not.

## Three clients, two integration contracts

| Client | Connection to the common Hosted A.X agent | What success proves |
| --- | --- | --- |
| Company Teams | Foundry Activity bridge to Responses, individual publish scope | Real 1:1 reply with Hosted execution and A.X inference trace |
| Microsoft 365 Copilot | Same Foundry Microsoft 365 publish flow and endpoint | Real Copilot agent conversation with correlated traces |
| GitHub Copilot CLI/app/IDE | Explicit `ask_ax` MCP tool delegates to the Hosted Responses endpoint | Copilot invokes the tool and receives the actual Hosted A.X result |

GitHub MCP delegation does **not** replace Copilot's main model, add A.X to its
native model picker, or prove all Copilot reasoning runs on A.X. The Copilot host
may process and summarize the returned text using its own model. Transmitting
company prompts/results into either service requires the applicable approvals.

`mcp_wrapper.py` is an **unregistered local-only** tool factory. Its tests use the
real MCP SDK tool dispatcher, Responses host and MAF, with a deterministic model
fixture; they do not use an actual Copilot client or cloud agent. Each call is
single-turn with `store=False`, no caller-supplied conversation/response IDs, no
automatic repo/history/file forwarding, a 4 KiB input bound, 64-token request,
64 KiB response bound, and one in-flight call. These are local contract limits,
not a deployed server-side quota. The confirmation argument is not human-consent
proof; the actual Copilot tool approval and exact text delegation must be enabled.

Before cloud use, replace the local-only restriction with an explicitly approved
fixed HTTPS agent endpoint and Entra audience/tenant authentication. Prefer a
local stdio wrapper using a user-authorized identity for the initial GitHub test;
do not embed tokens, share demo/GPU credentials, or expose an unauthenticated
remote MCP listener. A remote wrapper requires separate OAuth/MCP authorization,
least-privilege agent access and trusted caller isolation. Keep channel
conversations separate by default; cross-client history sharing is not approved.

Local cancellation closes the Responses HTTP transport. It does **not** prove
Hosted/GPU cancellation on disconnect. Before enabling long requests, verify
MCP cancellation -> owning Responses response cancel -> A.X relay/GPU abort,
using trusted caller-bound response IDs and bounded cleanup. No arbitrary
response-ID cancel tool is exposed by this experiment.

[GitHub's MCP documentation](https://docs.github.com/en/copilot/concepts/context/mcp)
supports CLI/app/IDE tools; for company Copilot Business/Enterprise, the
organization's **MCP servers in Copilot** policy must allow use. Actual policy,
client registration, app installation and all cloud permissions remain
unverified and require specific approval. The company Teams tenant gate still
blocks cloud deployment; adding GitHub as a target does not authorize a different
tenant, new resources or cross-tenant gateway.
