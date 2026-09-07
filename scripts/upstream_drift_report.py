#!/usr/bin/env python3
"""Generate a deterministic Git ancestry report for the Hermes fork."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit

_DEFAULT_UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"


@dataclass(frozen=True)
class Report:
    repository: str
    base_ref: str
    upstream_ref: str
    ahead: int
    behind: int
    base_sha: str
    upstream_sha: str
    merge_base_sha: str
    generated_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _redact_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        return raw.split("@", 1)[1] if "@" in raw else raw
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))
    except (TypeError, ValueError):
        return "<redacted-url>"


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _git_value(root: Path, args: Sequence[str]) -> str:
    result = _run_git(args, cwd=root)
    if result.returncode != 0:
        message = result.stderr.strip() or f"git {' '.join(args)} failed"
        raise RuntimeError(message)
    return result.stdout.strip()


def _ensure_upstream(root: Path, url: str, *, fetch: bool) -> None:
    current = _run_git(["remote", "get-url", "upstream"], cwd=root)
    if current.returncode != 0:
        added = _run_git(["remote", "add", "upstream", url], cwd=root)
        if added.returncode != 0:
            raise RuntimeError(added.stderr.strip() or "could not add upstream remote")
    elif current.stdout.strip() != url:
        raise RuntimeError(
            "existing upstream remote does not match the configured URL: "
            f"{_redact_url(current.stdout.strip())}"
        )

    if fetch:
        result = _run_git(
            ["fetch", "--prune", "--no-tags", "upstream", "main"],
            cwd=root,
            timeout=300.0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "could not fetch upstream/main"
            )


def build_report(
    repo: str | Path,
    *,
    base_ref: str,
    upstream_ref: str,
    fetch: bool = False,
    upstream_url: str = _DEFAULT_UPSTREAM_URL,
) -> Report:
    root = Path(repo).expanduser().resolve()
    if not (root / ".git").exists():
        probe = _run_git(["rev-parse", "--show-toplevel"], cwd=root)
        if probe.returncode != 0:
            raise RuntimeError(f"not a Git worktree: {root}")
        root = Path(probe.stdout.strip())

    _ensure_upstream(root, upstream_url, fetch=fetch)
    counts = _git_value(
        root,
        ["rev-list", "--left-right", "--count", f"{base_ref}...{upstream_ref}"],
    )
    try:
        ahead_text, behind_text = counts.split()
        ahead = int(ahead_text)
        behind = int(behind_text)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"unexpected rev-list output: {counts!r}") from exc

    return Report(
        repository=str(root),
        base_ref=base_ref,
        upstream_ref=upstream_ref,
        ahead=ahead,
        behind=behind,
        base_sha=_git_value(root, ["rev-parse", base_ref]),
        upstream_sha=_git_value(root, ["rev-parse", upstream_ref]),
        merge_base_sha=_git_value(root, ["merge-base", base_ref, upstream_ref]),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def render_markdown(report: Report) -> str:
    status = "Action required" if report.behind else "Current"
    return "\n".join(
        [
            "## Hermes upstream drift",
            "",
            f"**Status:** {status}",
            "",
            "| Field | Value |",
            "| --- | ---: |",
            f"| Fork-only commits | {report.ahead} |",
            f"| Upstream-only commits | {report.behind} |",
            f"| Fork ref | `{report.base_ref}` |",
            f"| Upstream ref | `{report.upstream_ref}` |",
            f"| Fork SHA | `{report.base_sha}` |",
            f"| Upstream SHA | `{report.upstream_sha}` |",
            f"| Merge base | `{report.merge_base_sha}` |",
            "",
            "When upstream-only commits are present, create a dated sync branch, "
            "merge or rebase upstream there, run the complete repository CI matrix, "
            "and verify the Neo4j, RTK, cache-foundation, and fork-operations tests "
            "before updating `main`.",
            "",
            f"Generated at `{report.generated_at}`.",
        ]
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", default="main")
    parser.add_argument("--upstream", default="upstream/main")
    parser.add_argument("--upstream-url", default=_DEFAULT_UPSTREAM_URL)
    parser.add_argument("--fetch", action="store_true")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--markdown", action="store_true")
    parser.add_argument(
        "--fail-if-behind",
        action="store_true",
        help="Return exit code 1 when upstream-only commits exist",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = build_report(
            args.repo,
            base_ref=args.base,
            upstream_ref=args.upstream,
            fetch=args.fetch,
            upstream_url=args.upstream_url,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"upstream drift report failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    elif args.markdown:
        print(render_markdown(report))
    else:
        print(f"fork ahead : {report.ahead}")
        print(f"fork behind: {report.behind}")
        print(f"fork SHA   : {report.base_sha}")
        print(f"upstream   : {report.upstream_sha}")

    if args.fail_if_behind and report.behind:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
