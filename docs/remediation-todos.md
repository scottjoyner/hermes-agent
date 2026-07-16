# hermes-agent Remediation TODOs (LLD §3.7)

Tracking for the unified-fleet hermes-agent review. Priority order from the
remediation brief: W-79, W-81, W-82, W-84 first; then W-77/W-78 extraction;
then W-80/W-83/W-85/W-86.

## Done this pass
| WI | What | Commit |
|----|------|--------|
| W-79 | `agent/factory.py:make_agent()` single construction path; `cli.py` routes its `AIAgent` wrapper through it. Gateway call sites left as phased TODO (import path differs). | 7626534 |
| W-81 | `tools/_auto_assign_client.py` shared client; `tools/auto_assign_tool.py` + `hermes_cli/auto_assign_worker.py` now import from it. | 517fcac |
| W-82 | `agent/fleet.py` hostnames/seed list + tailnet externalized to config/env; API-key forwarding gated behind `forward_api_keys`. | bb3e311 |
| W-84 | `config/loader.py` facade + `scripts/validate_config_example.py` CI drift gate (lint.yml). Known-drift baseline tracked. | 951dbe20 |
| W-77 | `gateway/run.py` header docstring + `gateway/README.md`; extracted `gateway/status.py`. | (this commit) |
| W-78 | Extracted `hermes_cli/colors.py` (light-mode remap helpers) from `cli.py`; re-imported. | (this commit) |
| W-85 | AGENTS.md tree updated: `gateway/run.py` (~18.5k), `cli.py` (~15k), `run_agent.py` (~4.4k), `hermes_cli/main.py` (~14k); added factory/loader entries. | (this commit) |

## Phased TODOs (not done this pass)
- **W-77 remaining**: extract `gateway/lifecycle.py` (startup/shutdown) and
  `gateway/dispatch.py` (message routing) from `gateway/run.py`. Full split is
  high-risk; see `gateway/README.md`.
- **W-78 remaining**: continue `cli.py` decomposition — remaining self-contained
  command groups still inline (e.g. banner, session picker). Each should move to
  `hermes_cli/` and re-import.
- **W-79 remaining**: route `gateway/run.py` (5 call sites: ~8504, ~11719,
  ~12193, ~16611) through `agent/factory.make_agent`. Requires care because
  gateway passes `**runtime_kwargs` dicts; factory already forwards `**overrides`
  so the switch is mechanical but must preserve the `runtime` dict shape.
- **W-80**: Finish Paperclip integration. Read `HANDOFF-PAPERCLIP-INTEGRATION.md`;
  add `PAPERCLIP_INTEGRATION_ENABLED` flag defaulting on adapter presence, with a
  log warning if "not yet hired". Don't break imports.
- **W-83**: Add Neo4j docker-compose (test profile) + live/CI smoke test for the
  knowledge-graph provider (`tests/plugins/memory/test_knowledge_graph.py`) that
  skips gracefully without Neo4j.
- **W-84 remaining**: fold the 17 `KEYS_KNOWN_DRIFT` entries (scripts/validate_config_example.py)
  into `hermes_cli/config.DEFAULT_CONFIG`, then delete them from the baseline until empty.
  Also route `gateway/run.py` raw YAML load through `config/loader.load_config()`.
- **W-85 remaining**: automate the doc-vs-tree lint (a script that checks AGENTS.md
  file sizes/entries against the real tree). Currently manual.
- **W-86**: remove legacy `MESSAGING_CWD`/`TERMINAL_CWD` reads (deprecation warnings
  exist); triage the ~1,343 TODO/NotImplemented markers — add a `grep -c` baseline
  to this doc and fix only clearly-stale v1 `raise NotImplementedError` stubs in
  `optional-skills/` templates if trivial.

## Marker baseline (W-86)
Run: `grep -rcn "TODO\|NotImplementedError" --include=*.py . | grep -v 0$` for a live count.
Captured 2026-07-16: TODO/NotImplemented markers ~1,343 (pre-existing; not modified
this pass).
