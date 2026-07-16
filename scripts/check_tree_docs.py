#!/usr/bin/env python3
"""Docs-vs-tree linter (LLD W-85).

Keeps AGENTS.md honest as large modules are extracted. It detects two kinds
of drift between AGENTS.md and the real tree:

1. LISTED-BUT-MISSING — AGENTS.md references a module path that does NOT
   exist on disk (path appears in the EXPECTED_MODULES list but is gone, e.g.
   after an extract/refactor).
2. PRESENT-BUT-UNLISTED — a real module above ``SIZE_THRESHOLD`` LOC that is
   NOT mentioned anywhere in AGENTS.md (a large file that drifted in without a
   doc update).

This is intentionally NON-BLOCKING by default: it exits 0 and prints
diagnostics so it can run in pre-commit / a nightly CI job without gating
merges. Pass ``--strict`` (or set ``CHECK_TREE_DOCS_STRICT=1``) to make
drift a hard failure (exit 1) once the tree stabilizes. The repo's CI wires
this as a separate, non-gating job — see ``scripts/check_tree_docs.py`` in
``AGENTS.md`` ("Docs-vs-Tree Lint").

Usage:
    python3 scripts/check_tree_docs.py [repo_root] [--strict]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Modules we expect AGENTS.md to reference by their top-level path. If any of
# these is listed in AGENTS.md but missing on disk, that's a broken reference
# (LISTED-BUT-MISSING). If it exists and is large but not mentioned, that's
# PRESENT-BUT-UNLISTED.
EXPECTED_MODULES = [
    "run_agent.py",
    "cli.py",
    "gateway/run.py",
    "hermes_cli/main.py",
    "agent/factory.py",
    "config/loader.py",
    "tools/_auto_assign_client.py",
    "agent/paperclip_integration.py",
]

# Real large modules that MUST be mentioned in AGENTS.md (drift sentinel).
# These are scanned for presence-and-size regardless of EXPECTED_MODULES; if a
# module here is >= SIZE_THRESHOLD LOC and absent from AGENTS.md, warn.
TRACKED_LARGE = [
    "run_agent.py",
    "cli.py",
    "hermes_state.py",
    "model_tools.py",
    "toolsets.py",
    "batch_runner.py",
    "gateway/run.py",
    "hermes_cli/main.py",
    "agent/factory.py",
    "config/loader.py",
    "tools/terminal_tool.py",
    "tools/file_tools.py",
]

SIZE_THRESHOLD = 2000  # LOC — modules larger than this should be listed.
AGENTS_MD = "AGENTS.md"
EXCLUDE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "website", "ui-tui"}


def _count_loc(path: Path) -> int:
    try:
        return sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
    except Exception:
        return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    strict = "--strict" in sys.argv or os.environ.get("CHECK_TREE_DOCS_STRICT") == "1"

    repo_root = Path(args[0]).resolve() if args else Path.cwd().resolve()
    agents_md = repo_root / AGENTS_MD
    if not agents_md.exists():
        print(f"[skip] {AGENTS_MD} not found in {repo_root}")
        return 0

    text = agents_md.read_text(encoding="utf-8", errors="replace")
    warnings = 0

    # 1. LISTED-BUT-MISSING + large-exists-not-mentioned for EXPECTED_MODULES.
    for mod in EXPECTED_MODULES:
        path = repo_root / mod
        if mod in text:
            if not path.exists():
                print(f"[warn] AGENTS.md references '{mod}' but it does not exist on disk.")
                warnings += 1
        elif path.exists():
            loc = _count_loc(path)
            if loc >= SIZE_THRESHOLD:
                print(f"[warn] '{mod}' is {loc} LOC but not mentioned in AGENTS.md.")
                warnings += 1

    # 2. PRESENT-BUT-UNLISTED for TRACKED_LARGE (the real large surface).
    for mod in TRACKED_LARGE:
        path = repo_root / mod
        if path.exists():
            loc = _count_loc(path)
            if loc >= SIZE_THRESHOLD and mod not in text:
                print(f"[warn] '{mod}' is {loc} LOC but not mentioned in AGENTS.md.")
                warnings += 1

    # 3. Any top-level .py above threshold not listed anywhere in AGENTS.md.
    for path in sorted(repo_root.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel.count("/") != 0:
            continue  # only top-level modules for the "missing" scan
        if rel == AGENTS_MD:
            continue
        parts = set(path.parts)
        if parts & EXCLUDE_DIRS:
            continue
        loc = _count_loc(path)
        if loc >= SIZE_THRESHOLD and rel not in text:
            print(f"[warn] top-level '{rel}' is {loc} LOC but not mentioned in AGENTS.md.")
            warnings += 1

    if warnings:
        print(f"\ndocs-vs-tree: {warnings} warning(s)."
              + (" (STRICT — failing)" if strict else " (non-blocking)"))
        return 1 if strict else 0
    print("docs-vs-tree: OK — AGENTS.md matches the tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
