"""Operational diagnostics for the scottjoyner Hermes Agent fork.

This bundled plugin is intentionally read-only by default. It inspects Git
remote topology, upstream drift, configuration posture, and the three custom
integrations carried by this fork. The only mutating option is the explicit
``--repair-remotes`` flag, which adds a missing ``upstream`` remote.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_constants import get_hermes_home
from utils import fast_safe_load

_DEFAULT_UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"
_DEFAULT_BACKUP_REF = "backup/pre-upstream-reconcile-2026-07-29"
_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CheckResult:
    """One bounded, secret-free diagnostic result."""

    name: str
    status: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DriftReport:
    """Git ancestry relationship between the published fork and upstream."""

    available: bool
    base_ref: str
    upstream_ref: str
    ahead: int = 0
    behind: int = 0
    base_sha: str = ""
    upstream_sha: str = ""
    merge_base_sha: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _repo_root(candidate: str | Path | None = None) -> Path | None:
    cwd = Path(candidate).expanduser() if candidate else Path.cwd()
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return Path(value) if value else None


def _git_value(root: Path, args: Sequence[str]) -> str:
    result = _run_git(args, cwd=root)
    return result.stdout.strip() if result.returncode == 0 else ""


def _ref_exists(root: Path, ref: str) -> bool:
    result = _run_git(["rev-parse", "--verify", "--quiet", ref], cwd=root)
    return result.returncode == 0


def _remote_url(root: Path, name: str) -> str:
    return _git_value(root, ["remote", "get-url", name])


def _upstream_url() -> str:
    return (
        os.getenv("HERMES_FORK_UPSTREAM_URL", "").strip()
        or _DEFAULT_UPSTREAM_URL
    )


def _ensure_upstream_remote(root: Path, *, repair: bool) -> tuple[str, str]:
    current = _remote_url(root, "upstream")
    expected = _upstream_url()
    if current or not repair:
        return current, ""
    result = _run_git(["remote", "add", "upstream", expected], cwd=root)
    if result.returncode != 0:
        error = result.stderr.strip() or "git remote add failed"
        return "", error
    return expected, ""


def _fetch_upstream(root: Path) -> str:
    result = _run_git(
        ["fetch", "--prune", "--no-tags", "upstream", "main"],
        cwd=root,
        timeout=180.0,
    )
    if result.returncode == 0:
        return ""
    return result.stderr.strip() or "git fetch upstream main failed"


def collect_drift(
    repo: str | Path | None = None,
    *,
    fetch: bool = False,
    repair_remotes: bool = False,
) -> DriftReport:
    """Return fork/upstream ancestry counts without changing branches."""

    root = _repo_root(repo)
    if root is None:
        return DriftReport(
            available=False,
            base_ref="",
            upstream_ref="",
            error="not inside a Git worktree",
        )

    upstream_url, remote_error = _ensure_upstream_remote(
        root,
        repair=repair_remotes,
    )
    if remote_error:
        return DriftReport(
            available=False,
            base_ref="",
            upstream_ref="upstream/main",
            error=remote_error,
        )
    if not upstream_url:
        return DriftReport(
            available=False,
            base_ref="",
            upstream_ref="upstream/main",
            error=(
                "missing upstream remote; run with --repair-remotes or add "
                f"{_upstream_url()}"
            ),
        )

    if fetch:
        fetch_error = _fetch_upstream(root)
        if fetch_error:
            return DriftReport(
                available=False,
                base_ref="",
                upstream_ref="upstream/main",
                error=fetch_error,
            )

    configured_base = os.getenv("HERMES_FORK_BASE_REF", "").strip()
    base_ref = configured_base or (
        "origin/main" if _ref_exists(root, "origin/main") else "main"
    )
    upstream_ref = (
        os.getenv("HERMES_FORK_UPSTREAM_REF", "").strip()
        or "upstream/main"
    )

    if not _ref_exists(root, base_ref):
        return DriftReport(
            available=False,
            base_ref=base_ref,
            upstream_ref=upstream_ref,
            error=f"base ref does not exist: {base_ref}",
        )
    if not _ref_exists(root, upstream_ref):
        return DriftReport(
            available=False,
            base_ref=base_ref,
            upstream_ref=upstream_ref,
            error=(
                f"upstream ref does not exist: {upstream_ref}; "
                "rerun with --fetch"
            ),
        )

    result = _run_git(
        ["rev-list", "--left-right", "--count", f"{base_ref}...{upstream_ref}"],
        cwd=root,
    )
    if result.returncode != 0:
        return DriftReport(
            available=False,
            base_ref=base_ref,
            upstream_ref=upstream_ref,
            error=result.stderr.strip() or "git rev-list failed",
        )

    try:
        ahead_text, behind_text = result.stdout.split()
        ahead = int(ahead_text)
        behind = int(behind_text)
    except (TypeError, ValueError):
        return DriftReport(
            available=False,
            base_ref=base_ref,
            upstream_ref=upstream_ref,
            error=f"unexpected git rev-list output: {result.stdout.strip()!r}",
        )

    return DriftReport(
        available=True,
        base_ref=base_ref,
        upstream_ref=upstream_ref,
        ahead=ahead,
        behind=behind,
        base_sha=_git_value(root, ["rev-parse", base_ref]),
        upstream_sha=_git_value(root, ["rev-parse", upstream_ref]),
        merge_base_sha=_git_value(root, ["merge-base", base_ref, upstream_ref]),
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = fast_safe_load(handle) or {}
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _add(
    checks: list[CheckResult],
    name: str,
    status: str,
    message: str,
    **details: Any,
) -> None:
    checks.append(
        CheckResult(
            name=name,
            status=status,
            message=message,
            details={key: value for key, value in details.items() if value != ""},
        )
    )


def collect_checks(
    repo: str | Path | None = None,
    *,
    fetch: bool = False,
    repair_remotes: bool = False,
) -> tuple[Path | None, list[CheckResult], DriftReport]:
    """Collect read-only operational checks for the fork and local profile."""

    checks: list[CheckResult] = []
    root = _repo_root(repo)
    if root is None:
        _add(checks, "git.worktree", "error", "not inside a Git worktree")
        drift = collect_drift(
            repo,
            fetch=fetch,
            repair_remotes=repair_remotes,
        )
        return None, checks, drift

    _add(checks, "git.worktree", "ok", "Git worktree detected", path=str(root))

    branch = _git_value(root, ["branch", "--show-current"]) or "detached"
    dirty = bool(_git_value(root, ["status", "--porcelain"]))
    _add(
        checks,
        "git.branch",
        "warning" if branch == "detached" else "ok",
        f"current branch: {branch}",
    )
    _add(
        checks,
        "git.clean",
        "warning" if dirty else "ok",
        "worktree has uncommitted changes" if dirty else "worktree is clean",
    )

    origin = _remote_url(root, "origin")
    if not origin:
        _add(checks, "git.origin", "error", "origin remote is missing")
    else:
        expected_fork = "scottjoyner/hermes-agent"
        status = "ok" if expected_fork.lower() in origin.lower() else "warning"
        _add(
            checks,
            "git.origin",
            status,
            "origin points to the maintained fork"
            if status == "ok"
            else "origin does not appear to point to scottjoyner/hermes-agent",
            url=origin,
        )

    upstream, remote_error = _ensure_upstream_remote(
        root,
        repair=repair_remotes,
    )
    if remote_error:
        _add(checks, "git.upstream", "error", remote_error)
    elif not upstream:
        _add(
            checks,
            "git.upstream",
            "warning",
            "upstream remote is missing",
            repair=(
                "hermes fork-doctor --repair-remotes --fetch"
            ),
        )
    else:
        expected = "NousResearch/hermes-agent"
        status = "ok" if expected.lower() in upstream.lower() else "warning"
        _add(
            checks,
            "git.upstream",
            status,
            "upstream points to NousResearch/hermes-agent"
            if status == "ok"
            else "upstream points to an unexpected repository",
            url=upstream,
        )

    drift = collect_drift(
        root,
        fetch=fetch,
        repair_remotes=repair_remotes,
    )
    if not drift.available:
        _add(checks, "git.drift", "warning", drift.error)
    elif drift.behind:
        _add(
            checks,
            "git.drift",
            "warning",
            f"fork is {drift.behind} commit(s) behind upstream",
            ahead=drift.ahead,
            behind=drift.behind,
        )
    else:
        _add(
            checks,
            "git.drift",
            "ok",
            "fork contains the current upstream ancestry",
            ahead=drift.ahead,
            behind=drift.behind,
        )

    backup_refs = (
        _DEFAULT_BACKUP_REF,
        f"origin/{_DEFAULT_BACKUP_REF}",
    )
    backup_present = any(_ref_exists(root, ref) for ref in backup_refs)
    _add(
        checks,
        "git.backup",
        "ok" if backup_present else "warning",
        "pre-reconciliation backup ref is available"
        if backup_present
        else "pre-reconciliation backup ref was not found locally",
        ref=_DEFAULT_BACKUP_REF,
    )

    home = get_hermes_home()
    config_path = home / "config.yaml"
    config = _load_yaml(config_path)
    _add(
        checks,
        "config.file",
        "ok" if config else "warning",
        "profile configuration loaded"
        if config
        else "profile config is missing or could not be parsed",
        path=str(config_path),
    )

    plugins_cfg = _mapping(config.get("plugins"))
    enabled_plugins = set(_list_of_strings(plugins_cfg.get("enabled")))
    for plugin_name in ("rtk-rewrite", "cache-foundation"):
        enabled = plugin_name in enabled_plugins
        _add(
            checks,
            f"plugin.{plugin_name}",
            "ok" if enabled else "warning",
            f"{plugin_name} is enabled"
            if enabled
            else f"{plugin_name} is disabled",
            enable=f"hermes plugins enable {plugin_name}",
        )

    rtk_binary = shutil.which(os.getenv("HERMES_RTK_BINARY", "").strip() or "rtk")
    _add(
        checks,
        "rtk.binary",
        "ok" if rtk_binary else "warning",
        "RTK binary is available"
        if rtk_binary
        else "RTK binary is not available on PATH",
        binary=rtk_binary or "",
        repair="hermes rtk install",
    )

    memory_cfg = _mapping(config.get("memory"))
    memory_provider = str(memory_cfg.get("provider") or "").strip()
    kg_active = memory_provider == "knowledge_graph"
    _add(
        checks,
        "memory.provider",
        "ok" if kg_active else "warning",
        "knowledge_graph is the active external memory provider"
        if kg_active
        else "knowledge_graph is not the active external memory provider",
        provider=memory_provider or "built-in only",
        repair="hermes memory setup",
    )

    kg_config = _mapping(config.get("knowledge_graph"))
    kg_config.update(_load_json(home / "knowledge_graph.json"))
    kg_uri = str(os.getenv("NEO4J_URI") or kg_config.get("uri") or "").strip()
    kg_password_present = bool(os.getenv("NEO4J_PASSWORD", "").strip())
    if kg_active:
        _add(
            checks,
            "memory.neo4j_uri",
            "ok" if kg_uri else "error",
            "Neo4j URI is configured" if kg_uri else "Neo4j URI is missing",
            uri=kg_uri,
        )
        _add(
            checks,
            "memory.neo4j_password",
            "ok" if kg_password_present else "error",
            "Neo4j password is available from the environment"
            if kg_password_present
            else "NEO4J_PASSWORD is not available",
        )

    cache_enabled = "cache-foundation" in enabled_plugins
    stable_prefix = os.getenv("HERMES_CACHE_STABLE_PREFIX_FILE", "").strip()
    if cache_enabled:
        prefix_path = Path(stable_prefix).expanduser() if stable_prefix else None
        prefix_ready = bool(prefix_path and prefix_path.is_file())
        _add(
            checks,
            "cache.stable_prefix",
            "ok" if prefix_ready else "warning",
            "stable-prefix file is configured"
            if prefix_ready
            else "stable-prefix file is not configured or readable",
            path=str(prefix_path) if prefix_path else "",
        )
        remote_cache = _truthy(os.getenv("HERMES_CACHE_ALLOW_REMOTE"))
        _add(
            checks,
            "cache.remote_disclosure",
            "warning" if remote_cache else "ok",
            "remote cache identifier disclosure is explicitly enabled"
            if remote_cache
            else "remote cache identifier disclosure is disabled",
        )

    model_cfg = _mapping(config.get("model"))
    provider = str(model_cfg.get("provider") or "auto").strip().lower()
    local_provider = provider in {
        "custom",
        "lmstudio",
        "ollama",
        "vllm",
        "llamacpp",
    }
    context_length = model_cfg.get("context_length")
    if local_provider:
        _add(
            checks,
            "model.context_length",
            "ok" if context_length else "warning",
            "local model context length is explicit"
            if context_length
            else "local model context length is not explicit",
            context_length=context_length or "",
        )

    terminal_cfg = _mapping(config.get("terminal"))
    terminal_backend = str(terminal_cfg.get("backend") or "local").strip()
    _add(
        checks,
        "terminal.isolation",
        "ok" if terminal_backend != "local" else "warning",
        f"terminal backend is {terminal_backend}",
        recommendation=(
            "prefer ssh or docker for stronger isolation"
            if terminal_backend == "local"
            else ""
        ),
    )

    worktree_enabled = bool(config.get("worktree"))
    _add(
        checks,
        "git.worktree_isolation",
        "ok" if worktree_enabled else "warning",
        "automatic Git worktree isolation is enabled"
        if worktree_enabled
        else "automatic Git worktree isolation is disabled",
    )

    return root, checks, drift


def _status_counts(checks: Sequence[CheckResult]) -> dict[str, int]:
    counts = {"ok": 0, "warning": 0, "error": 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    return counts


def _doctor_payload(
    root: Path | None,
    checks: Sequence[CheckResult],
    drift: DriftReport,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": str(root) if root else "",
        "summary": _status_counts(checks),
        "drift": drift.to_dict(),
        "checks": [check.to_dict() for check in checks],
    }


def _print_doctor(payload: Mapping[str, Any]) -> None:
    symbols = {"ok": "OK", "warning": "WARN", "error": "ERROR"}
    print("Hermes fork diagnostics")
    print("=======================")
    if payload.get("repository"):
        print(f"repository: {payload['repository']}")
    print()
    for check in payload.get("checks", []):
        status = str(check.get("status") or "warning")
        label = symbols.get(status, status.upper())
        print(f"[{label:5}] {check.get('name')}: {check.get('message')}")
        details = check.get("details")
        if isinstance(details, Mapping):
            for key, value in details.items():
                print(f"        {key}: {value}")
    summary = payload.get("summary", {})
    print()
    print(
        "summary: "
        f"{summary.get('ok', 0)} ok, "
        f"{summary.get('warning', 0)} warning(s), "
        f"{summary.get('error', 0)} error(s)"
    )


def _setup_doctor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="Git worktree to inspect")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch upstream/main before calculating drift",
    )
    parser.add_argument(
        "--repair-remotes",
        action="store_true",
        help="Add a missing upstream remote before inspection",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero for warnings as well as errors",
    )
    parser.set_defaults(func=_handle_doctor)


def _handle_doctor(args: argparse.Namespace) -> int:
    root, checks, drift = collect_checks(
        getattr(args, "repo", "."),
        fetch=bool(getattr(args, "fetch", False)),
        repair_remotes=bool(getattr(args, "repair_remotes", False)),
    )
    payload = _doctor_payload(root, checks, drift)
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_doctor(payload)
    summary = payload["summary"]
    if summary.get("error", 0):
        return 1
    if bool(getattr(args, "strict", False)) and summary.get("warning", 0):
        return 1
    return 0


def _setup_drift(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="Git worktree to inspect")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch upstream/main before calculating drift",
    )
    parser.add_argument(
        "--repair-remotes",
        action="store_true",
        help="Add a missing upstream remote before inspection",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when the fork is behind upstream",
    )
    parser.set_defaults(func=_handle_drift)


def _handle_drift(args: argparse.Namespace) -> int:
    report = collect_drift(
        getattr(args, "repo", "."),
        fetch=bool(getattr(args, "fetch", False)),
        repair_remotes=bool(getattr(args, "repair_remotes", False)),
    )
    if bool(getattr(args, "json", False)):
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif report.available:
        print(f"base ref     : {report.base_ref} ({report.base_sha[:12]})")
        print(
            f"upstream ref : {report.upstream_ref} "
            f"({report.upstream_sha[:12]})"
        )
        print(f"ahead        : {report.ahead}")
        print(f"behind       : {report.behind}")
        print(f"merge base   : {report.merge_base_sha[:12]}")
    else:
        print(f"fork drift unavailable: {report.error}", file=sys.stderr)
        return 2
    if bool(getattr(args, "strict", False)) and report.behind:
        return 1
    return 0


def register(ctx: Any) -> None:
    """Register fork diagnostics without changing agent runtime behavior."""

    register_cli = getattr(ctx, "register_cli_command", None)
    if not callable(register_cli):
        return
    register_cli(
        "fork-doctor",
        "Inspect fork remotes, drift, integrations, and configuration posture",
        _setup_doctor,
        _handle_doctor,
        description="Operational diagnostics for the maintained Hermes fork",
    )
    register_cli(
        "fork-drift",
        "Compare the published fork with NousResearch upstream",
        _setup_drift,
        _handle_drift,
        description="Read-only upstream ancestry and drift report",
    )


__all__ = [
    "CheckResult",
    "DriftReport",
    "collect_checks",
    "collect_drift",
    "register",
]
