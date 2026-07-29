# Fork reconciliation plan — 2026-07-29

## Point-in-time state

- Fork default branch before reconciliation: `scottjoyner/hermes-agent@e88963ad6e52444ae9b6ab12b770e3f7e75112a3`
- Initial upstream audit SHA: `NousResearch/hermes-agent@41a07f5b8451f88a8b8b5adfc0cfdc2ada0a1f90`
- Refreshed upstream baseline SHA: `NousResearch/hermes-agent@b6729ba90552f11ac1064c3c7dcb7ef20361ef8c`
- Original merge base: `2517917de34eeb6a40f5a17a2e59d9746803dfa5`
- Original fork divergence: 27 commits ahead and 9,585 commits behind upstream.

Safety branches created before reconciliation:

- `backup/pre-upstream-reconcile-2026-07-29` — exact pre-reconciliation fork state.
- `reconcile/upstream-2026-07-29` — initial audited upstream baseline.
- `reconcile/upstream-2026-07-29-current` — refreshed upstream baseline after 11 additional upstream commits landed during the port.

The refreshed reconciliation branch was created directly from the upstream SHA above. The 11 intervening upstream commits affect voice, cron, desktop, and related tests; they do not modify the memory-provider API or memory plugin loader.

## Decision

Do **not** merge the old fork `main` into the new upstream baseline. The amount of upstream churn makes a conventional conflict-by-conflict merge high-risk and preserves obsolete file layouts.

Use `reconcile/upstream-2026-07-29-current` as the new baseline. Re-port only durable capabilities as isolated plugins/providers/skills, each in its own branch and pull request.

## Preserve and re-port

### 1. Neo4j knowledge graph memory

Preserve the `plugins/memory/knowledge_graph` capability, but adapt it to the current public memory-provider contract:

- Implement `sync_turn(user_content, assistant_content, session_id=..., messages=...)` and route the current message list into graph capture.
- Do not depend on the fork-only `on_turn_recorded` core hook.
- Use the `hermes_home` value passed to `initialize()` for profile isolation.
- Keep Neo4j and embedding configuration provider-local.
- Make reasoning, raw tool arguments, and raw tool results opt-in and disabled by default.
- Retain durable queueing, document indexing, embedding failover, full-text fallback, and focused tests.

### 2. Local-model fleet and delegation

Preserve the behavior, not `agent/fleet.py` as a core patch.

Rebuild this as a model-provider/router plugin that:

- Discovers explicitly configured OpenAI-compatible endpoints.
- Records health, latency, context length, model capabilities, and available headroom.
- Routes weak-model helper tasks to the cheapest adequate node.
- Uses upstream delegation/subagent APIs rather than patching the conversation loop.
- Never forwards API keys unless explicitly enabled.
- Applies per-task token, latency, and cost budgets.

### 3. OpenCode integration

Upstream now has OpenCode skills and provider surfaces. Reconcile the fork's OpenCode client against those APIs and retain only missing functionality, especially fleet-aware delegation and structured result capture.

### 4. Sophia voice

Keep as a standalone plugin after validating its current plugin manifest, dashboard API, and entrypoint tests. Avoid changes to core CLI/TUI files.

### 5. External systems

- Paperclip: external adapter/plugin, not core.
- RuView: MCP or plugin exposing read-only sensing/status tools and event subscriptions; keep device-specific dependencies outside Hermes core.
- Exo-inspired self-improvement: adopt an immutable event log, sandbox snapshots/rewind, proposal branches, test gates, bounded lineage, and budget-aware acceptance criteria. Do not allow direct self-modification of the protected baseline.

### 6. User skills

Re-add NAS, USB sorting, and auto-ingest skills only after deduplicating their repeated templates/scripts. Prefer user-installed skills under `HERMES_HOME` when they are machine-specific.

## Treat as superseded unless a failing test proves otherwise

- `cli.py` and `hermes_cli/main.py` extraction/refactor commits.
- `gateway/dispatch.py`, `gateway/lifecycle.py`, and old `gateway/status.py` extraction work.
- The fork config-loader facade and config example drift gates.
- Old Kanban auto-assignment core patches; upstream has evolved Kanban worker-lane behavior.
- TUI event/config patches tied to the May 2026 layout.
- Fork copies of `AGENTS.md`, setup scripts, lockfiles, and CI workflows.
- Personal host defaults and tailnet assumptions.

## Validation sequence

```bash
git fetch origin
git switch reconcile/upstream-2026-07-29-current

uv venv ~/.hermes/venvs/hermes-dev --python 3.11
source ~/.hermes/venvs/hermes-dev/bin/activate
uv pip install -e ".[all,dev]"

scripts/run_tests.sh
hermes --help
hermes doctor
```

After the clean upstream baseline passes, port each preserved capability in a separate branch and run its focused tests plus the upstream suite.

## Replacing the default branch after validation

The backup branch must remain in place. Then move `main` to the validated reconciliation branch with a lease-protected update:

```bash
git fetch origin
git switch main
git reset --hard origin/reconcile/upstream-2026-07-29-current
git push --force-with-lease origin main
```

Do not perform this step until the clean upstream baseline and required deployment smoke tests pass.
