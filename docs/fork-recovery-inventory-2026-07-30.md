# Fork recovery inventory — 2026-07-30

The reconciliation merge deliberately selected the validated upstream-based tree
instead of performing a line-by-line merge of a fork that was roughly 9,600
commits behind. That prevented obsolete core code from returning, but it also
removed fork-only files that had not yet been re-ported.

The original tree remains available at:

```text
backup/pre-upstream-reconcile-2026-07-29
```

Use it as evidence and design input. Do not merge or copy the branch wholesale.

## Already recovered against current APIs

| Capability | Current implementation |
| --- | --- |
| Neo4j semantic/relational memory | `plugins/memory/knowledge_graph/` |
| RTK command-output compression | `plugins/rtk-rewrite/` |
| Cache identity, affinity, warmup, and telemetry | `plugins/cache-foundation/` plus `agent/cache_foundation.py` |
| Small-model always-on context reduction | `plugins/small-model-context/` and root `.hermes.md` in the current PR |

## Priority 1 — re-port, do not restore verbatim

### Configurable inference fleet and Headroom router

Historical source:

```text
backup/pre-upstream-reconcile-2026-07-29:agent/fleet.py
```

Why it matters:

- discovers OpenAI-compatible and Ollama-style local endpoints;
- catalogs available models;
- routes a request to another machine;
- directly supports the goal of using several heterogeneous local nodes.

Why the old implementation cannot return unchanged:

- its fallback tailnet and seed hosts contain personal machine defaults;
- discovery probes hosts and ports sequentially;
- routing is primarily model-name match plus latency;
- node capabilities are mostly inferred rather than measured;
- configuration and API-key forwarding include environment-variable surfaces
  that conflict with current repository policy;
- it does not integrate current provider transports, middleware, context
  budgets, cache manifests, or session affinity.

Required replacement contract:

1. Explicit node registry in `config.yaml`; optional discovery may add candidates
   but never invent personal hosts.
2. Concurrent bounded health/model probes with backoff and durable observations.
3. Per-node model, quantization, tokenizer, chat-template, context, throughput,
   queue depth, cold-load, vision, reasoning, and cache-checkpoint metadata.
4. Route scoring constrained by task adequacy first, then context fit, cache
   affinity, queue/prefill/cold-load cost, throughput, latency, and monetary cost.
5. No credential forwarding unless explicitly enabled for a named node.
6. Session stickiness by default; migration only with a compatible replay or
   verified checkpoint identity.
7. Public provider/router interface rather than patches throughout the agent
   loop.
8. Headroom transforms only the volatile conversation zone and reports exactly
   what it removed or summarized.
9. Deterministic fallback to the configured primary endpoint when fleet state is
   unavailable.
10. Benchmarks that measure quality, time-to-first-token, tokens/second, prompt
    processing, cache hits, and completion rate on small models.

This should be the next major implementation PR after context optimization.

## Priority 2 — inspect and salvage narrowly

### LM Studio setup convenience

Historical source originated in PR #1 and modified `setup-hermes.sh` to detect
and start a local LM Studio endpoint. Current setup and provider configuration
have changed substantially. Re-evaluate the user experience against the current
`hermes setup` and model-provider plugin flows, then port only missing behavior.

Safe target:

- detect an already-running endpoint;
- list models through the provider's supported API;
- write non-secret settings to `config.yaml`;
- keep installation/download actions explicit and non-interactive-safe;
- avoid assuming one model is appropriate from RAM/GPU size alone.

### OpenCode integration

Historical files include:

```text
agent/opencode_client.py
```

Determine whether current ACP, Codex app-server, MCP, terminal, or delegation
interfaces already cover the workflow. If a gap remains, implement it as a
standalone provider/plugin or CLI + skill rather than restoring a bespoke core
client.

### Paperclip integration

Historical files include:

```text
agent/paperclip_integration.py
HANDOFF-PAPERCLIP-INTEGRATION.md
```

First verify that the external project and protocol are still active and that
current Kanban/delegation APIs do not supersede it. Keep third-party orchestration
outside the core tree unless a generic interface has multiple real consumers.

## Do not restore wholesale

### Historical `agent/factory.py`

Current upstream agent construction and initialization have been heavily
refactored. Recover individual invariants only when a current failing path proves
they are missing. Reintroducing the old factory would create a second agent
construction path and configuration drift.

### Old gateway, CLI, TUI, configuration, CI, lockfile, and setup patches

These areas changed by thousands of upstream commits. Treat old diffs as bug
reports or UX requirements, not merge candidates. Reproduce each desired
behavior on current `main`, then fix the current implementation at its public
seam with focused tests.

## Recovery workflow

For each candidate capability:

1. Read the historical file and commits from the backup branch.
2. State the user-visible behavior worth preserving.
3. Trace the equivalent current subsystem and public extension interfaces.
4. Identify security, privacy, caching, and configuration-policy differences.
5. Build one focused branch from current `main`.
6. Add behavior-contract and integration tests.
7. Run the complete repository CI matrix before merge.
8. Record which historical behavior was intentionally not restored.

This inventory should be updated whenever a displaced feature is recovered or
formally retired.
