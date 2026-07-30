# Hermes fleet offline integration — 2026-07-30

Hermes is the primary execution provider for the reconciled AssistX fleet. It is an executor and agent runtime, not an inference inventory, scheduler, recovery controller, network discovery service, or model loader.

## Required flow

```text
AssistX claim + fenced task
  -> Hermes worker
  -> strict-offline auto-router endpoint
  -> AssistX-admitted physical runtime/model
  -> router selects approved LAN path or Tailscale fallback
  -> checkpoint/result/evidence returned with claim ID
```

## Provider policy

For this deployment:

- use the local `auto-router` OpenAI-compatible endpoint;
- exclude Nous Portal, OpenRouter, OpenAI, Cerebras, Groq, Grok, Anthropic, and every other hosted inference provider;
- do not fail open to a hosted provider;
- do not choose a raw LM Studio/llama.cpp endpoint independently when AssistX supplied a route;
- do not discover LAN, LM Studio Link, or Tailscale endpoints independently;
- do not invoke model load/unload as part of ordinary task execution;
- do not reinterpret LM Studio Link's localhost view as physical runtime ownership.

## Authority boundary

Hermes may:

- claim a task through AssistX;
- execute tools within the task's bounded contract;
- report heartbeats, checkpoints, progress, artifacts, and completion;
- provide measured execution evidence;
- request a route or a stronger admitted local model.

Hermes may not:

- assign work to itself outside the canonical claim lifecycle;
- maintain a competing node/model/access-path registry;
- alter node priority or health state directly;
- start, stop, load, unload, or restart inference without a typed AssistX action;
- promote its own repository changes;
- route to public inference.

## OpenCode relationship

OpenCode is a separate, lower-priority executor integration. It may receive explicitly assigned repository work, but it is not a fallback inference provider and must not change Hermes or fleet routing configuration. Disabling OpenCode must not change the inference path used by Hermes.

## Normal deployment settings

Configure Hermes with one model provider entry pointing to the local gateway, for example:

```text
base URL: http://auto-router:8088/v1
API key: local-offline-only
model alias: auto/code or auto/high-quality
```

The exact Hermes configuration surface may evolve with upstream. The invariant is more important than the file format: the only model endpoint visible to the fleet Hermes worker should be the strict-offline gateway.

Hermes never sees or chooses the runtime's LAN and Tailscale URLs. AssistX approves those paths and the router selects the first reachable one while sharing one physical-runtime capacity gate across every path.

## Shadow migration settings

The old production executor remains running while the control plane and router are validated. The shadow Hermes adapter must therefore remain off until identity, capacity, network-path, state-authority, restart, and rollback gates pass.

The matching `auto-assist` reconciliation Compose file places `hermes-adapter` behind the explicit `executor` profile. When approved for one synthetic task, use:

```text
AssistX shadow URL: http://api:8000 inside the reconciliation network
Router shadow URL: http://auto-router-reconciliation:8088/v1
Shared Docker network: assistx_reconciliation_shared
Host-published AssistX URL: http://127.0.0.1:18000
Host-published router URL: http://127.0.0.1:18088/v1
Model alias: local/reconciliation-default
Self-task generation: disabled
Max tasks per loop: 1
Recovery execution: disabled
Repository mutation: disabled
```

The container-to-container route avoids exposing AssistX or the router on `0.0.0.0`. The router itself may reach AssistX-approved RFC1918 and Tailscale runtime paths through normal outbound bridge networking. The machine-side cutover gate must prove those paths from inside the router container.

The shadow Hermes agent may execute only a synthetic task. It must not inspect or modify production repositories, start another agent, load a model, or continue running after the gate unless the operator approves extended shadow operation.

See the matching `auto-assist` branch:

- `docs/FULL_AUTO_RECONCILIATION_20260730.md` for architecture and acceptance gates;
- `docs/LOCAL_AGENT_LIVE_MIGRATION_RUNBOOK_20260730.md` for the exact side-by-side sequence;
- `docs/TAILSCALE_RUNTIME_ACCESS_20260730.md` for LAN preference and Tailscale fallback evidence;
- `docs/LOCAL_AGENT_HANDOFF_20260730.md` for local-agent authority and evidence requirements;
- `deploy/reconciliation/system-inventory.yaml` for the full required system/service inventory.
