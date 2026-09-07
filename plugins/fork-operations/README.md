# Fork operations

This bundled, opt-in plugin helps operate the maintained
`scottjoyner/hermes-agent` fork without patching the agent loop or provider
runtime.

It adds two read-only-by-default commands:

```bash
hermes fork-doctor
hermes fork-drift
```

## Enable

```bash
hermes plugins enable fork-operations
```

Restart Hermes so the CLI plugin is rediscovered.

## Recommended first run

```bash
hermes fork-doctor --repair-remotes --fetch
```

`--repair-remotes` performs one narrowly scoped mutation: when the Git checkout
has no `upstream` remote, it adds:

```text
https://github.com/NousResearch/hermes-agent.git
```

It never rewrites an existing remote. `--fetch` updates `upstream/main` without
switching branches, merging, rebasing, or touching the worktree.

## What `fork-doctor` checks

- the current Git worktree, branch, and clean/dirty status;
- `origin` points to `scottjoyner/hermes-agent`;
- `upstream` points to `NousResearch/hermes-agent`;
- fork-ahead and fork-behind ancestry counts;
- the pre-reconciliation backup ref remains available;
- profile-local `config.yaml` can be read;
- `rtk-rewrite` and `cache-foundation` are enabled;
- the RTK binary is available;
- `knowledge_graph` is the active external memory provider;
- Neo4j URI and password presence when that provider is active;
- stable-prefix file posture and remote cache disclosure;
- explicit local-model context length;
- terminal isolation and automatic Git worktree isolation.

No secret values are printed. Credential checks report only presence or absence.
The command does not connect to Neo4j or an inference endpoint; use
`kg_status`, `hermes rtk doctor`, and `hermes cache doctor` for live integration
checks.

Machine-readable output:

```bash
hermes fork-doctor --json
```

By default, warnings do not make the command fail. Use `--strict` in CI or a
release checklist:

```bash
hermes fork-doctor --fetch --strict
```

## Upstream drift

```bash
hermes fork-drift --fetch
```

The report compares the published fork ref with upstream without changing the
current branch. Resolution order:

- base: `HERMES_FORK_BASE_REF`, then `origin/main`, then `main`;
- upstream: `HERMES_FORK_UPSTREAM_REF`, then `upstream/main`.

Use JSON for automation:

```bash
hermes fork-drift --fetch --json
```

Use `--strict` to return exit code 1 when upstream-only commits exist.
Configuration or Git errors return exit code 2.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `HERMES_FORK_UPSTREAM_URL` | URL used only when `--repair-remotes` adds a missing remote |
| `HERMES_FORK_BASE_REF` | Fork ref to compare, such as `origin/main` |
| `HERMES_FORK_UPSTREAM_REF` | Upstream ref to compare, such as `upstream/main` |

## Boundaries

This plugin does not:

- pull or merge upstream commits;
- force-update any branch;
- modify an existing remote URL;
- enable other plugins automatically;
- print API keys or passwords;
- claim that cache warmup is a persisted KV checkpoint.

The scheduled repository workflow uses the independent
`scripts/upstream_drift_report.py` helper so drift monitoring continues even
when this plugin is disabled on a particular Hermes profile.
