# Hermes fleet offline integration — 2026-07-30

Hermes is the primary execution provider for the reconciled AssistX fleet. It is an executor and agent runtime, not an inference inventory, scheduler, recovery controller, network discovery service, physical runtime router, or model loader.

## Required flow

```text
AssistX claim + fenced task
  -> Hermes worker in fleet.mode=external
  -> semantic auto/* model intent + task metadata
  -> strict-offline auto-router endpoint
  -> AssistX-admitted physical runtime/model
  -> router selects approved LAN path or Tailscale fallback
  -> checkpoint/result/evidence returned with claim ID
```

## Required operating mode

The reconciled deployment must use:

```yaml
model:
  provider: custom
  default: auto/code
  base_url: http://auto-router-reconciliation:8088/v1

fleet:
  mode: external
  external:
    base_url: http://auto-router-reconciliation:8088/v1
    admin_url: http://auto-router-reconciliation:8088
    admin_token_env: AUTO_ROUTER_ADMIN_TOKEN
    default_model: auto/code
    strict_offline: true
```

External mode is a hard authority boundary:

- `fleet.nodes` is forbidden;
- Hermes does not probe physical endpoints;
- Hermes does not track node health, inflight requests, or concurrency slots;
- Hermes does not choose physical models or LAN/Tailscale paths;
- `hermes fleet serve` and `hermes fleet discover` are disabled;
- `hermes fleet status` reads auto-router health, model intents, and authenticated admission state;
- `hermes fleet route` explains only the semantic `auto/*` intent.

The standalone Headroom proxy remains available through `fleet.mode=standalone` only for installations that do not deploy AssistX and auto-router. It must never be enabled in the reconciled stack.

## Provider policy

For this deployment:

- use the local `auto-router` OpenAI-compatible endpoint;
- select semantic aliases such as `auto/code`, `auto/review`, `auto/iterate`, or `auto/finalize`;
- exclude Nous Portal, OpenRouter, OpenAI, Cerebras, Groq, Grok, Anthropic, Gemini, hosted Mistral, Cloudflare inference, and every other hosted inference provider;
- do not fail open to a hosted provider;
- do not choose a raw LM Studio/llama.cpp endpoint independently;
- do not discover LAN, LM Studio Link, or Tailscale endpoints independently;
- do not invoke model load/unload as part of ordinary task execution;
- do not reinterpret LM Studio Link's localhost view as physical runtime ownership.

## Request metadata contract

Hermes must preserve task and cache identity through its normal provider transport. At minimum, carry where available:

```text
task_id
agent_run_id
claim_id / fencing token
priority
required_capabilities
local_only=true
workflow_stage
session_id
checkpoint_id
cache identity headers
```

Hermes supplies intent and execution context. auto-router performs semantic policy selection, physical runtime admission, bounded queueing, and access-path selection.

## Authority boundary

Hermes may:

- claim a task through AssistX;
- execute tools within the task's bounded contract;
- report heartbeats, checkpoints, progress, artifacts, and completion;
- provide measured execution evidence;
- request a semantic local route or a stronger admitted local policy alias;
- read router health and admission state for operator visibility.

Hermes may not:

- assign work to itself outside the canonical claim lifecycle;
- maintain a competing node/model/access-path registry;
- maintain a competing slot, inflight, queue, or health authority;
- alter node priority or health state directly;
- start, stop, load, unload, or restart inference without a typed AssistX action;
- promote its own repository changes;
- route to public inference;
- start its standalone fleet proxy in the reconciled deployment.

## OpenCode relationship

OpenCode is a separate, lower-priority executor integration. It may receive explicitly assigned repository work, but it is not a fallback inference provider and must not change Hermes or fleet routing configuration. Disabling OpenCode must not change the inference path used by Hermes.

## Shadow migration settings

The old production executor remains running while the control plane and router are validated. The shadow Hermes adapter must therefore remain off until identity, capacity, signed-projection, network-path, state-authority, containment, air-gap restore, restart, and rollback gates pass.

The matching `auto-assist` reconciliation Compose file places `hermes-adapter` behind the explicit `executor` profile. When approved for one synthetic task, use:

```text
AssistX shadow URL: http://api:8000 inside the reconciliation network
Router shadow URL: http://auto-router-reconciliation:8088/v1
Router admin URL: http://auto-router-reconciliation:8088
Shared Docker network: assistx_reconciliation_shared
Host-published AssistX URL: http://127.0.0.1:18000
Host-published router URL: http://127.0.0.1:18088/v1
Model intent: auto/code
Fleet mode: external
Self-task generation: disabled
Max tasks per loop: 1
Recovery execution: disabled
Repository mutation: disabled except one explicitly mounted task worktree
```

The executor must run non-root with a read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, no Docker socket, no SSH identity, no broad repository/NAS/SSD mounts, no public-provider credentials, no web/browser/MCP tools, and at most one operator-approved worktree.

The router itself may reach AssistX-approved RFC1918 and Tailscale runtime paths through normal outbound bridge networking. The machine-side cutover gate must prove both paths from inside the router container, LAN preference, Tailscale fallback, return to LAN, and one shared admission counter.

The shadow Hermes agent may execute only a synthetic task. It must not inspect or modify production repositories, start another agent, load a model, or continue running after the gate unless the operator approves extended shadow operation.

See the matching `auto-assist` branch:

- `docs/FINAL_CUTOVER_OPERATOR_PACKET_20260730.md` for the final sequence;
- `docs/FULL_AUTO_RECONCILIATION_20260730.md` for architecture and acceptance gates;
- `docs/LOCAL_AGENT_LIVE_MIGRATION_RUNBOOK_20260730.md` for the side-by-side sequence;
- `docs/TAILSCALE_RUNTIME_ACCESS_20260730.md` for LAN preference and Tailscale fallback evidence;
- `deploy/reconciliation/hermes-config.yaml.example` for the exact external-mode config;
- `deploy/reconciliation/runtime-projection.example.yaml` for operator-approved runtime admission;
- `deploy/reconciliation/final-cutover-evidence.example.yaml` for the final fail-closed evidence contract.
