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
| Small-model always-on context reduction | `plugins/small-model-context/` and root `.hermes.md` in PR #10 |
| Configurable local inference fleet and Headroom routing | `plugins/fleet-router/` in PR #10 |

## Recovered in PR #10 — configurable fleet and Headroom router

Historical source:

```text
backup/pre-upstream-reconcile-2026-07-29:agent/fleet.py
```

The useful historical behavior was endpoint cataloging and routing requests to
other local machines. The old implementation was not restored because:

- its fallback tailnet and seed hosts contained personal machine defaults;
- discovery probed hosts and ports sequentially;
- routing was primarily model-name match plus latency;
- node capabilities were mostly inferred rather than measured;
- API-key forwarding included broad environment-variable fallbacks;
- it patched an in-process provider path that no longer matches current Hermes
  transports, middleware, cache identity, or configuration policy.

The replacement is a standalone OpenAI-compatible local proxy. Hermes keeps one
stable provider endpoint while the proxy owns the heterogeneous node registry.

Implemented contract:

1. Nodes are listed explicitly in `config.yaml`; no host, tailnet, subnet, or
   port scanning occurs implicitly.
2. Public endpoints are rejected unless the named node sets
   `allow_remote: true`.
3. Health and model-catalog probes execute concurrently with bounded workers and
   short per-node timeouts.
4. Hard routing constraints cover model or alias availability, tools, vision,
   reasoning, and configured context capacity.
5. Route scoring covers context headroom, exact model match, operator priority,
   configured prefill/decode throughput, measured latency EMA, current in-flight
   concurrency, session affinity, and cache-checkpoint affinity.
6. Client-facing model aliases map to different upstream model IDs without
   rewriting the Hermes conversation.
7. The proxy supports normal and streaming `/v1/chat/completions` responses and
   exposes `/v1/models`, `/health`, and `/fleet/status`.
8. A failed connection or upstream 5xx can retry another eligible node within a
   bounded attempt count. Concrete 4xx responses are relayed immediately rather
   than duplicating work.
9. Inbound authorization is never forwarded. Each protected node may name one
   dedicated `api_key_env`; unrelated provider keys are ignored.
10. Cache identity headers pass through, allowing the router to keep session and
    checkpoint affinity while compatible inference servers consume the exact
    cache manifest.
11. The proxy binds to loopback by default. Non-loopback binding requires an
    explicit configuration gate and may require a dedicated inbound token.
12. `hermes fleet status`, `discover`, `doctor`, `route`, and `serve` provide
    operator inspection without adding model-visible tools or prompt overhead.

Intentionally not claimed or implemented yet:

- exact tokenizer, chat-template, engine-build, quantization, and KV-layout
  compatibility discovery;
- transfer of KV tensors between independent inference engines;
- automatic model download, synchronization, loading, or eviction;
- subnet or Tailscale control-plane discovery;
- durable multi-process health and throughput observations;
- tokenizer-exact request sizing; the first implementation uses a conservative
  tokenizer-independent estimate and configured context limits;
- monetary-cost routing or quality benchmarking;
- Headroom transformations that summarize or remove conversation content. The
  router currently selects only nodes that can fit the request as sent.

Those are follow-on improvements to the current public proxy boundary, not a
reason to restore the historical core file.

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
5. Build one focused branch from current `main` or extend an explicitly approved
   in-flight PR when the capabilities share one deployment boundary.
6. Add behavior-contract and integration tests.
7. Run the complete repository CI matrix before merge.
8. Record which historical behavior was intentionally not restored.

This inventory should be updated whenever a displaced feature is recovered or
formally retired.
