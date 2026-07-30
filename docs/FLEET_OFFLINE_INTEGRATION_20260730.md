# Hermes fleet offline integration — 2026-07-30

Hermes is the primary execution provider for the reconciled AssistX fleet. It is an executor and agent runtime, not an inference inventory, scheduler, recovery controller, or model loader.

## Required flow

```text
AssistX claim + fenced task
  -> Hermes worker
  -> strict-offline auto-router endpoint
  -> AssistX-admitted local runtime/model
  -> checkpoint/result/evidence returned with claim ID
```

## Provider policy

For this deployment:

- use the local `auto-router` OpenAI-compatible endpoint;
- exclude Nous Portal, OpenRouter, OpenAI, Cerebras, Groq, Grok, Anthropic, and every other hosted inference provider;
- do not fail open to a hosted provider;
- do not choose a raw LM Studio/llama.cpp endpoint independently when AssistX supplied a route;
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
- maintain a competing node/model registry;
- alter node priority or health state directly;
- start, stop, load, unload, or restart inference without a typed AssistX action;
- promote its own repository changes;
- route to public inference.

## OpenCode relationship

OpenCode is a separate, lower-priority executor integration. It may receive explicitly assigned repository work, but it is not a fallback inference provider and must not change Hermes or fleet routing configuration. Disabling OpenCode must not change the inference path used by Hermes.

## Initial deployment settings

Configure Hermes with one model provider entry pointing to the local gateway, for example:

```text
base URL: http://auto-router:8088/v1
API key: local-router
model alias: auto/code or auto/high-quality
```

The exact Hermes configuration surface may evolve with upstream. The invariant is more important than the file format: the only model endpoint visible to the fleet Hermes worker should be the strict-offline gateway.

See `auto-assist/docs/FULL_AUTO_RECONCILIATION_20260730.md` on the matching branch for the canonical architecture and acceptance gates.
