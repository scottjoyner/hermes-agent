"""Operational diagnostics for the maintained Scott Joyner Hermes fork.

The plugin is read-only by default. ``--repair-remotes`` is the only mutating
option and adds a missing ``upstream`` remote without rewriting an existing one.
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
from urllib.parse import urlsplit, urlunsplit

from hermes_constants import get_hermes_home
from utils import fast_safe_load

_DEFAULT_UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"
_DEFAULT_BACKUP_REF = "backup/pre-upstream-reconcile-2026-07-29"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}
_SECRET_URL_KEYS = {"url", "uri"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DriftReport:
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
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return bool(value)


def _redact_url(value: Any) -> str:
    """Remove URL credentials, query strings, and fragments."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        # Git's scp-like syntax: user@host:owner/repo.git. The user segment is
        # not needed for diagnostics and can contain a token in unusual setups.
        if "@" in raw:
            return raw.split("@", 1)[1]
        return raw
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))
    except (ValueError, TypeError):
        return "<redacted-url>"


def _safe_details(details: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in details.items():
        if value == "":
            continue
        result[key] = _redact_url(value) if key in _SECRET_URL_KEYS else value
    return result


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
    value = result.stdout.strip() if result.returncode == 0 else ""
    return Path(value) if value else None


def _git_value(root: Path, args: Sequence[str]) -> str:
    result = _run_git(args, cwd=root)
    return result.stdout.strip() if result.returncode == 0 else ""


def _ref_exists(root: Path, ref: str) -> bool:
    return (
        _run_git(["rev-parse", "--verify", "--quiet", ref], cwd=root).returncode
        == 0
    )


def _remote_url(root: Path, name: str) -> str:
    return _git_value(root, ["remote", "get-url", name])


def _upstream_url() -> str:
    return os.getenv("HERMES_FORK_UPSTREAM_URL", "").strip() or _DEFAULT_UPSTREAM_URL


def _ensure_upstream_remote(root: Path, *, repair: bool) -> tuple[str, str]:
    current = _remote_url(root, "upstream")
    if current or not repair:
        return current, ""
    expected = _upstream_url()
    result = _run_git(["remote", "add", "upstream", expected], cwd=root)
    if result.returncode != 0:
        return "", result.stderr.strip() or "git remote add failed"
    return expected, ""


def _fetch_upstream(root: Path) -> str:
    result = _run_git(
        ["fetch", "--prune", "--no-tags", "upstream", "main"],
        cwd=root,
        timeout=180.0,
    )
    return "" if result.returncode == 0 else (
        result.stderr.strip() or "git fetch upstream main failed"
    )


def collect_drift(
    repo: str | Path | None = None,
    *,
    fetch: bool = False,
    repair_remotes: bool = False,
) -> DriftReport:
    root = _repo_root(repo)
    if root is None:
        return DriftReport(False, "", "", error="not inside a Git worktree")

    upstream_url, remote_error = _ensure_upstream_remote(
        root,
        repair=repair_remotes,
    )
    if remote_error:
        return DriftReport(False, "", "upstream/main", error=remote_error)
    if not upstream_url:
        return DriftReport(
            False,
            "",
            "upstream/main",
            error=(
                "missing upstream remote; run with --repair-remotes or add "
                f"{_redact_url(_upstream_url())}"
            ),
        )
    if fetch:
        fetch_error = _fetch_upstream(root)
        if fetch_error:
            return DriftReport(False, "", "upstream/main", error=fetch_error)

    base_ref = os.getenv("HERMES_FORK_BASE_REF", "").strip() or (
        "origin/main" if _ref_exists(root, "origin/main") else "main"
    )
    upstream_ref = (
        os.getenv("HERMES_FORK_UPSTREAM_REF", "").strip() or "upstream/main"
    )
    if not _ref_exists(root, base_ref):
        return DriftReport(
            False,
            base_ref,
            upstream_ref,
            error=f"base ref does not exist: {base_ref}",
        )
    if not _ref_exists(root, upstream_ref):
        return DriftReport(
            False,
            base_ref,
            upstream_ref,
            error=f"upstream ref does not exist: {upstream_ref}; rerun with --fetch",
        )

    result = _run_git(
        ["rev-list", "--left-right", "--count", f"{base_ref}...{upstream_ref}"],
        cwd=root,
    )
    if result.returncode != 0:
        return DriftReport(
            False,
            base_ref,
            upstream_ref,
            error=result.stderr.strip() or "git rev-list failed",
        )
    try:
        ahead_text, behind_text = result.stdout.split()
        ahead, behind = int(ahead_text), int(behind_text)
    except (TypeError, ValueError):
        return DriftReport(
            False,
            base_ref,
            upstream_ref,
            error=f"unexpected git rev-list output: {result.stdout.strip()!r}",
        )

    return DriftReport(
        True,
        base_ref,
        upstream_ref,
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


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _add(
    checks: list[CheckResult],
    name: str,
    status: str,
    message: str,
    **details: Any,
) -> None:
    checks.append(CheckResult(name, status, message, _safe_details(details)))


def collect_checks(
    repo: str | Path | None = None,
    *,
    fetch: bool = False,
    repair_remotes: bool = False,
) -> tuple[Path | None, list[CheckResult], DriftReport]:
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
        expected = "scottjoyner/hermes-agent"
        ok = expected.lower() in origin.lower()
        _add(
            checks,
            "git.origin",
            "ok" if ok else "warning",
            "origin points to the maintained fork"
            if ok
            else "origin does not appear to point to scottjoyner/hermes-agent",
            url=origin,
        )

    upstream, remote_error = _ensure_upstream_remote(root, repair=repair_remotes)
    if remote_error:
        _add(checks, "git.upstream", "error", remote_error)
    elif not upstream:
        _add(
            checks,
            "git.upstream",
            "warning",
            "upstream remote is missing",
            repair="hermes fork-doctor --repair-remotes --fetch",
        )
    else:
        expected = "NousResearch/hermes-agent"
        ok = expected.lower() in upstream.lower()
        _add(
            checks,
            "git.upstream",
            "ok" if ok else "warning",
            "upstream points to NousResearch/hermes-agent"
            if ok
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
    else:
        _add(
            checks,
            "git.drift",
            "warning" if drift.behind else "ok",
            f"fork is {drift.behind} commit(s) behind upstream"
            if drift.behind
            else "fork contains the current upstream ancestry",
            ahead=drift.ahead,
            behind=drift.behind,
        )

    backup_present = any(
        _ref_exists(root, ref)
        for ref in (_DEFAULT_BACKUP_REF, f"origin/{_DEFAULT_BACKUP_REF}")
    )
    _add(
        checks,
        "git.backup",
        "ok" if backup_present else "warning",
        "pre-reconciliation backup ref is available"
        if backup_present
        else "pre-reconciliation backup ref was not found locally",
        ref=_DEFAULT_BACKUP_REF,
    )

    home = Path(get_hermes_home())
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

    enabled = set(_strings(_mapping(config.get("plugins")).get("enabled")))
    for plugin_name in ("rtk-rewrite", "cache-foundation"):
        active = plugin_name in enabled
        _add(
            checks,
            f"plugin.{plugin_name}",
            "ok" if active else "warning",
            f"{plugin_name} is enabled"
            if active
            else f"{plugin_name} is disabled",
            enable=f"hermes plugins enable {plugin_name}",
        )

    rtk = shutil.which(os.getenv("HERMES_RTK_BINARY", "").strip() or "rtk")
    _add(
        checks,
        "rtk.binary",
        "ok" if rtk else "warning",
        "RTK binary is available" if rtk else "RTK binary is not available on PATH",
        binary=rtk or "",
        repair="hermes rtk install",
    )

    memory = _mapping(config.get("memory"))
    provider = str(memory.get("provider") or "").strip()
    kg_active = provider == "knowledge_graph"
    _add(
        checks,
        "memory.provider",
        "ok" if kg_active else "warning",
        "knowledge_graph is the active external memory provider"
        if kg_active
        else "knowledge_graph is not the active external memory provider",
        provider=provider or "built-in only",
        repair="hermes memory setup",
    )
    if kg_active:
        kg = _mapping(config.get("knowledge_graph"))
        kg.update(_load_json(home / "knowledge_graph.json"))
        uri = str(os.getenv("NEO4J_URI") or kg.get("uri") or "").strip()
        password_present = bool(os.getenv("NEO4J_PASSWORD", "").strip())
        _add(
            checks,
            "memory.neo4j_uri",
            "ok" if uri else "error",
            "Neo4j URI is configured" if uri else "Neo4j URI is missing",
            uri=uri,
        )
        _add(
            checks,
            "memory.neo4j_password",
            "ok" if password_present else "error",
            "Neo4j password is available from the environment"
            if password_present
            else "NEO4J_PASSWORD is not available",
        )

    if "cache-foundation" in enabled:
        raw_prefix = os.getenv("HERMES_CACHE_STABLE_PREFIX_FILE", "").strip()
        prefix = Path(raw_prefix).expanduser() if raw_prefix else None
        ready = bool(prefix and prefix.is_file())
        _add(
            checks,
            "cache.stable_prefix",
            "ok" if ready else "warning",
            "stable-prefix file is configured"
            if ready
            else "stable-prefix file is not configured or readable",
            path=str(prefix) if prefix else "",
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

    model = _mapping(config.get("model"))
    model_provider = str(model.get("provider") or "auto").strip().lower()
    if model_provider in {"custom", "lmstudio", "ollama", "vllm", "llamacpp"}:
        context_length = model.get("context_length")
        _add(
            checks,
            "model.context_length",
            "ok" if context_length else "warning",
            "local model context length is explicit"
            if context_length
            else "local model context length is not explicit",
            context_length=context_length or "",
        )

    terminal = _mapping(config.get("terminal"))
    backend = str(terminal.get("backend") or "local").strip()
    _add(
        checks,
        "terminal.isolation",
        "ok" if backend != "local" else "warning",
        f"terminal backend is {backend}",
        recommendation=(
            "prefer ssh or docker for stronger isolation" if backend == "local" else ""
        ),
    )
    worktree_enabled = _truthy(config.get("worktree"))
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
        print(
            f"[{symbols.get(status, status.upper()):5}] "
            f"{check.get('name')}: {check.get('message')}"
        )
        details = check.get("details")
        if isinstance(details, Mapping):
            for key, value in details.items():
                print(f"        {key}: {value}")
    summary = payload.get("summary", {})
    print()
    print(
        f"summary: {summary.get('ok', 0)} ok, "
        f"{summary.get('warning', 0)} warning(s), "
        f"{summary.get('error', 0)} error(s)"
    )


def _setup_doctor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="Git worktree to inspect")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--repair-remotes", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
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
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--repair-remotes", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
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
        print(f"upstream ref : {report.upstream_ref} ({report.upstream_sha[:12]})")
        print(f"ahead        : {report.ahead}")
        print(f"behind       : {report.behind}")
        print(f"merge base   : {report.merge_base_sha[:12]}")
    else:
        print(f"fork drift unavailable: {report.error}", file=sys.stderr)
        return 2
    return 1 if bool(getattr(args, "strict", False)) and report.behind else 0


def register(ctx: Any) -> None:
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
