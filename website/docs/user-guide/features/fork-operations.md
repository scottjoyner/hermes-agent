---
title: "Operating the Scott Joyner fork"
description: "Configure custom Hermes extensions, detect upstream drift, and keep fork deployments safe to update"
---

# Operating the Scott Joyner fork

The `scottjoyner/hermes-agent` fork follows NousResearch upstream while carrying
three intentionally maintained extensions:

- Neo4j knowledge-graph memory;
- RTK terminal command/output compression;
- cache-aware inference manifests, affinity, warmup, and telemetry.

The fork also ships an operational plugin and a scheduled GitHub workflow to
make configuration omissions and upstream drift visible before they become a
large reconciliation problem.

## First-run checklist

From the fork checkout:

```bash
hermes plugins enable fork-operations
hermes plugins enable rtk-rewrite
hermes plugins enable cache-foundation
hermes rtk install
hermes memory setup
```

Select `knowledge_graph` in the memory setup flow, then restart Hermes.

Run the combined diagnostics:

```bash
hermes fork-doctor --repair-remotes --fetch
```

Complete live checks separately:

```bash
hermes rtk doctor
hermes cache doctor --endpoint http://localhost:1234/v1 --provider lmstudio
hermes memory status
```

`fork-doctor` checks local posture without connecting to Neo4j or an inference
server. The integration-specific commands verify those live services.

## Recommended Git remotes

```text
origin    git@github.com:scottjoyner/hermes-agent.git
upstream  https://github.com/NousResearch/hermes-agent.git
```

`hermes fork-doctor --repair-remotes` adds `upstream` only when it is missing.
It never rewrites an existing URL.

Never merge upstream directly into a production checkout. Use a dated branch:

```bash
git fetch upstream main
git switch -c sync/upstream-YYYY-MM-DD main
git merge --no-ff upstream/main
```

Resolve conflicts on that branch, run the complete CI matrix, and verify the
custom integration tests before opening a PR into the fork's `main`.

## Scheduled drift reporting

The repository workflow `.github/workflows/upstream-drift.yml` runs every
Monday and can also be dispatched manually. It:

1. fetches `NousResearch/hermes-agent`;
2. computes fork-only and upstream-only ancestry counts;
3. writes a Markdown summary to the workflow run;
4. opens or refreshes one `[upstream-drift]` issue when the fork is behind;
5. closes that issue automatically when upstream-only drift returns to zero.

The workflow does not merge, rebase, or push code.

For a local or CI report:

```bash
python scripts/upstream_drift_report.py --fetch --markdown
```

Use `--fail-if-behind` when upstream drift should fail a release gate.

## Profile-local configuration

Hermes loads `$HERMES_HOME/.env` before a project `.env`. The profile-local
file is recommended for Neo4j, embedding, and cache credentials.

```bash
NEO4J_PASSWORD=replace-me
HERMES_KG_EMBEDDINGS_API_KEY=
HERMES_CACHE_API_KEY=
```

Do not set `HERMES_CACHE_ALLOW_REMOTE=1` unless the destination is a specifically
trusted cache-aware proxy. The setting discloses opaque session and cache
identifiers to that endpoint.

A baseline `config.yaml` for a local-model deployment:

```yaml
model:
  provider: lmstudio
  default: local-model-id
  base_url: http://127.0.0.1:1234/v1
  context_length: 65536

plugins:
  enabled:
    - fork-operations
    - rtk-rewrite
    - cache-foundation
    - security-guidance

memory:
  provider: knowledge_graph

knowledge_graph:
  enabled: true
  uri: bolt://127.0.0.1:7687
  user: neo4j
  database: neo4j
  embeddings_base_urls:
    - http://xwing:1234/v1
    - http://tie:1234/v1
  embeddings_model: nomic-embed-text
  capture_reasoning: false
  capture_tool_arguments: false
  capture_tool_results: false

worktree: true
worktree_sync: true

terminal:
  backend: ssh
  ssh_host: xwing
  ssh_user: scott
  ssh_key: ~/.ssh/id_ed25519
  cwd: /home/scott/git
  timeout: 180
```

Set `model.context_length` explicitly for local servers when their model catalog
does not accurately expose the configured context window.

## RTK boundary

RTK rewrites eligible terminal commands before execution so noisy output is
filtered before entering model context. The bundled plugin remains fail-open:
missing RTK, timeout, malformed output, or an unsupported command runs the
original command unchanged.

Do not also run `rtk init --agent hermes`; a user-local plugin with the same name
would override the bundled integration.

## Knowledge-graph boundary

Only one external memory provider can be active. Selecting `knowledge_graph`
disables Honcho, OpenViking, Mem0, and other external providers for that
profile, while built-in `MEMORY.md` and `USER.md` remain active.

Keep these defaults unless sensitive capture is deliberate:

```yaml
knowledge_graph:
  capture_reasoning: false
  capture_tool_arguments: false
  capture_tool_results: false
```

Back up both Neo4j and `$HERMES_HOME/knowledge_graph/pending.db`. The SQLite
queue can contain durable writes that have not reached Neo4j yet.

See [Neo4j knowledge-graph memory](./knowledge-graph-memory.md).

## Cache-foundation boundary

The cache plugin currently supplies deterministic identifiers, affinity,
warmup, and telemetry. It does not serialize or restore KV tensors and does not
route a live session across machines.

For a plain local OpenAI-compatible server, configure an exact stable-prefix
file:

```bash
HERMES_CACHE_STABLE_PREFIX_FILE=~/.hermes/cache/prompts/primary-cli-coding.txt
```

The file is used only when its contents exactly match the beginning of the
outgoing system prompt.

A future llama.cpp or fleet adapter must validate the model, quantization,
tokenizer, chat template, engine build, context size, and KV layout before
claiming a checkpoint restore.

## Release checklist

Before promoting an upstream-sync or extension PR:

```bash
hermes fork-doctor --fetch --strict
hermes rtk doctor
hermes cache status --json
hermes memory status
pytest -q tests/plugins/test_fork_operations_plugin.py
pytest -q tests/plugins/test_rtk_rewrite_plugin.py
pytest -q tests/plugins/test_cache_foundation_plugin.py
pytest -q tests/plugins/memory/test_knowledge_graph_provider.py
```

Also run the repository's full GitHub Actions matrix. Workflow-file changes
require the repository's `ci-reviewed` PR label.

## Recovery references

Retain these references until several successful upstream syncs have completed:

```text
backup/pre-upstream-reconcile-2026-07-29
post-reconcile-2026-07-29
```

The backup branch preserves the old fork tree. A post-reconciliation tag should
identify the first validated production baseline after PR #8.
