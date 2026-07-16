#!/usr/bin/env python3
"""Validate cli-config.yaml.example against DEFAULT_CONFIG (LLD W-84).

CI step: every key present in the shipped example config should also exist in
the canonical ``DEFAULT_CONFIG`` (hermes_cli/config.py). This catches the
three-loader drift described in AGENTS.md — a key documented in the example
but missing from DEFAULT_CONFIG means the gateway/CLI may never see it.

The gate currently allows a known baseline of drift (KEYS_KNOWN_DRIFT) — keys
that exist only in the CLI loader's private defaults and have not yet been
folded into DEFAULT_CONFIG. New drift (keys not in this list) fails the build.
As W-84 progresses and the CLI defaults are merged into DEFAULT_CONFIG, entries
should be removed from KEYS_KNOWN_DRIFT until it is empty.

Exits non-zero on NEW drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Known baseline of example keys not yet in DEFAULT_CONFIG (CLI-only defaults).
# Tracked as a phased TODO in docs/remediation-todos.md (W-84).
KEYS_KNOWN_DRIFT = {
    "model",
    "terminal.lifetime_seconds",
    "memory.nudge_interval",
    "memory.flush_min_turns",
    "session_reset",
    "group_sessions_per_user",
    "streaming",
    "skills.creation_nudge_interval",
    "agent.verbose",
    "agent.reasoning_effort",
    "agent.personalities",
    "platform_toolsets",
    "code_execution.timeout",
    "code_execution.max_tool_calls",
    "display.tool_progress",
    "display.cleanup_progress",
    "display.background_process_notifications",
}


def main() -> int:
    example_path = REPO_ROOT / "cli-config.yaml.example"
    if not example_path.exists():
        print(f"skip: {example_path} not found", file=sys.stderr)
        return 0

    sys.path.insert(0, str(REPO_ROOT))
    from config.loader import validate_example_against_defaults

    missing = validate_example_against_defaults(str(example_path))
    if not missing:
        print("OK: cli-config.yaml.example keys are a subset of DEFAULT_CONFIG")
        return 0

    new_drift = [m for m in missing if m not in KEYS_KNOWN_DRIFT]
    if new_drift:
        print("FAIL: cli-config.yaml.example has NEW keys missing from DEFAULT_CONFIG:")
        for path in new_drift:
            print(f"  - {path}")
        return 1

    print("WARN: example keys missing from DEFAULT_CONFIG are all in known-drift baseline:")
    for path in missing:
        print(f"  - {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
