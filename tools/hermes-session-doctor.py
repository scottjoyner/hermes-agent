#!/usr/bin/env python3
"""Read-only Hermes/OpenCode session health doctor.

Reports launch invariants and the OpenCode-vs-Hermes auth boundary without
printing credential values. It never edits config, auth stores, or databases.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


# The installed wrapper is executed from arbitrary working directories. Keep
# imports bound to this Hermes checkout rather than relying on cwd/PYTHONPATH.
HERMES_ROOT = Path(__file__).resolve().parents[1]
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))


def command_version() -> tuple[bool, str]:
    exe = shutil.which("opencode")
    if not exe:
        return False, "not_found"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        return out.returncode == 0, (out.stdout or out.stderr).strip().splitlines()[0][:200]
    except Exception as exc:
        return False, type(exc).__name__


def auth_summary(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "providers": []}
    try:
        data = json.loads(path.read_text())
        providers = sorted(data) if isinstance(data, dict) else []
        return {"exists": True, "mode": oct(path.stat().st_mode & 0o777), "providers": providers}
    except Exception as exc:
        return {"exists": True, "providers": [], "parse_error": type(exc).__name__}


def db_check(path: Path) -> dict:
    result = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return result
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        result["integrity_check"] = integrity
        result["ok"] = integrity == "ok"
        con.close()
    except Exception as exc:
        result.update({"ok": False, "error": type(exc).__name__})
    return result


def provider_check() -> dict:
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider(requested="opencode-zen", target_model="space-bunny-free")
        return {"ok": True, "provider": runtime.get("provider"), "source": runtime.get("source")}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc).splitlines()[0][:240]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    version_ok, version = command_version()
    auth = auth_summary(Path.home() / ".local/share/opencode/auth.json")
    checks = {
        "hermes_session_launchable": True,
        "opencode_launchable": version_ok,
        "opencode_version": version,
        "provider_resolution": provider_check(),
        "opencode_auth_store": auth,
        "session_db_integrity": db_check(Path.home() / ".local/share/opencode/opencode.db"),
        "failed_invariants": [],
    }
    if not version_ok:
        checks["failed_invariants"].append("opencode_not_launchable")
    if not checks["provider_resolution"].get("ok"):
        checks["failed_invariants"].append("opencode_zen_not_resolvable_by_hermes")
    if not checks["session_db_integrity"].get("ok"):
        checks["failed_invariants"].append("opencode_session_db_integrity")
    checks["hermes_session_launchable"] = not bool(checks["failed_invariants"])
    if args.as_json:
        print(json.dumps(checks, indent=2, sort_keys=True))
    else:
        print(json.dumps(checks, indent=2, sort_keys=True))
    return 0 if checks["hermes_session_launchable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
