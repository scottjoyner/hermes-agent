"""Hermes integration for RTK (Rust Token Killer).

The plugin rewrites eligible ``terminal`` commands through ``rtk rewrite``
before Hermes executes them. RTK remains the source of truth for command
coverage and output filtering; this module only adapts Hermes lifecycle and CLI
surfaces. Every runtime failure is fail-open so an unavailable or unhealthy RTK
binary never blocks the original command.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

_ACCEPTED_REWRITE_RETURN_CODES = {0, 3}
_EXPECTED_PASSTHROUGH_RETURN_CODES = {1, 2}
_DEFAULT_TIMEOUT_SECONDS = 2.0
_RTK_REPOSITORY = "https://github.com/rtk-ai/rtk"

_binary_checked = False
_cached_binary: Optional[str] = None
_missing_warned = False


@dataclass(frozen=True)
class RewriteResult:
    """Result of asking RTK whether a command should be rewritten."""

    original: str
    rewritten: str
    changed: bool
    returncode: int
    stderr: str = ""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _plugin_disabled() -> bool:
    return _truthy(os.getenv("HERMES_RTK_DISABLE", ""))


def _timeout_seconds() -> float:
    raw = os.getenv("HERMES_RTK_REWRITE_TIMEOUT", "")
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return max(0.1, min(float(raw), 10.0))
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS


def _warn(message: str) -> None:
    print(f"rtk-rewrite: {message}", file=sys.stderr)


def _reset_binary_cache() -> None:
    global _binary_checked, _cached_binary
    _binary_checked = False
    _cached_binary = None


def _find_binary() -> Optional[str]:
    """Resolve the configured RTK binary once per process."""

    global _binary_checked, _cached_binary
    if _binary_checked:
        return _cached_binary

    configured = os.getenv("HERMES_RTK_BINARY", "").strip()
    _cached_binary = shutil.which(configured or "rtk")
    _binary_checked = True
    return _cached_binary


def _run_rtk(
    argv: Sequence[str],
    *,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess[str]:
    binary = _find_binary()
    if not binary:
        raise FileNotFoundError("rtk binary was not found in PATH")
    return subprocess.run(
        [binary, *argv],
        shell=False,
        timeout=timeout,
        capture_output=True,
        text=True,
        check=False,
    )


def rewrite_command(command: str) -> RewriteResult:
    """Return RTK's rewrite decision without executing the command."""

    original = command
    result = _run_rtk(
        ["rewrite", command],
        timeout=_timeout_seconds(),
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode in _EXPECTED_PASSTHROUGH_RETURN_CODES:
        return RewriteResult(
            original=original,
            rewritten=original,
            changed=False,
            returncode=result.returncode,
            stderr=stderr,
        )
    if result.returncode not in _ACCEPTED_REWRITE_RETURN_CODES:
        details = f"rtk rewrite failed with exit {result.returncode}"
        if stderr:
            details += f": {stderr}"
        raise RuntimeError(details)

    rewritten = stdout or original
    return RewriteResult(
        original=original,
        rewritten=rewritten,
        changed=rewritten != original,
        returncode=result.returncode,
        stderr=stderr,
    )


def _pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> None:
    """Rewrite a mutable Hermes terminal payload and otherwise fail open."""

    if _plugin_disabled() or tool_name != "terminal" or not isinstance(args, dict):
        return

    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return

    try:
        decision = rewrite_command(command)
    except subprocess.TimeoutExpired:
        _warn("rtk rewrite timed out; running the original command")
        return
    except FileNotFoundError:
        _warn("rtk disappeared from PATH; running the original command")
        _reset_binary_cache()
        return
    except Exception as exc:
        _warn(f"{exc}; running the original command")
        return

    if decision.changed:
        args["command"] = decision.rewritten


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    subcommands = parser.add_subparsers(dest="rtk_command")

    subcommands.add_parser(
        "status",
        help="Show RTK availability and Hermes rewrite status",
    )
    subcommands.add_parser(
        "doctor",
        help="Run RTK integration diagnostics",
    )

    install = subcommands.add_parser(
        "install",
        help="Install RTK with Homebrew or Cargo",
    )
    install.add_argument(
        "--method",
        choices=("auto", "brew", "cargo"),
        default="auto",
        help="Installation method (default: auto)",
    )

    gain = subcommands.add_parser(
        "gain",
        help="Show RTK token-savings analytics",
    )
    gain.add_argument(
        "--graph",
        action="store_true",
        help="Show RTK's ASCII savings graph",
    )

    rewrite = subcommands.add_parser(
        "rewrite",
        help="Preview how RTK rewrites a shell command",
    )
    rewrite.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to preview, for example: git status",
    )

    parser.set_defaults(func=_handle_cli)


def _version_line() -> tuple[bool, str]:
    binary = _find_binary()
    if not binary:
        return False, "not installed"
    try:
        result = _run_rtk(["--version"], timeout=5)
    except Exception as exc:
        return False, f"unavailable: {exc}"
    value = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return False, value or f"exit {result.returncode}"
    return True, value or "installed"


def _cmd_status() -> int:
    available, version = _version_line()
    payload = {
        "available": available,
        "binary": _find_binary(),
        "version": version,
        "rewrite_enabled": available and not _plugin_disabled(),
        "disabled_by_env": _plugin_disabled(),
        "timeout_seconds": _timeout_seconds(),
    }
    print(json.dumps(payload, indent=2))
    return 0 if available else 1


def _cmd_doctor() -> int:
    available, version = _version_line()
    print("RTK / Hermes diagnostics")
    print("------------------------")
    print(f"binary   : {_find_binary() or 'not found'}")
    print(f"version  : {version}")
    print(f"disabled : {'yes' if _plugin_disabled() else 'no'}")
    print(f"timeout  : {_timeout_seconds():g}s")
    if not available:
        print("result   : not ready — run `hermes rtk install`")
        return 1

    try:
        decision = rewrite_command("git status")
    except Exception as exc:
        print(f"rewrite  : failed ({exc})")
        return 1

    print(f"rewrite  : {decision.rewritten}")
    if not decision.changed:
        print("result   : RTK returned passthrough for the probe")
        return 1
    print("result   : ready")
    return 0


def _install_command(method: str) -> Optional[list[str]]:
    selected = method
    if selected == "auto":
        if shutil.which("brew"):
            selected = "brew"
        elif shutil.which("cargo"):
            selected = "cargo"
        else:
            return None

    if selected == "brew":
        if not shutil.which("brew"):
            raise RuntimeError("Homebrew is not installed")
        return ["brew", "install", "rtk"]
    if selected == "cargo":
        if not shutil.which("cargo"):
            raise RuntimeError("Cargo is not installed")
        return ["cargo", "install", "--git", _RTK_REPOSITORY]
    return None


def _cmd_install(method: str) -> int:
    if _find_binary():
        print(f"RTK is already installed at {_find_binary()}")
        return 0

    try:
        command = _install_command(method)
    except RuntimeError as exc:
        print(f"rtk install: {exc}")
        return 1

    if not command:
        print("No supported package manager was found.")
        print("Install RTK from https://github.com/rtk-ai/rtk#installation")
        return 1

    print(f"$ {shlex.join(command)}")
    result = subprocess.run(command, shell=False, check=False)
    _reset_binary_cache()
    if result.returncode != 0:
        print(f"rtk install failed with exit {result.returncode}")
        return result.returncode or 1
    if not _find_binary():
        print("RTK installed but is not yet visible in PATH; restart your shell.")
        return 1
    print(f"RTK ready at {_find_binary()}")
    return 0


def _cmd_gain(graph: bool) -> int:
    argv = ["gain"]
    if graph:
        argv.append("--graph")
    try:
        result = _run_rtk(argv, timeout=30)
    except Exception as exc:
        print(f"rtk gain: {exc}")
        return 1
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return result.returncode


def _cmd_rewrite(command_parts: Sequence[str]) -> int:
    if not command_parts:
        print("usage: hermes rtk rewrite <command>")
        return 2
    command = command_parts[0] if len(command_parts) == 1 else shlex.join(command_parts)
    try:
        decision = rewrite_command(command)
    except Exception as exc:
        print(f"rtk rewrite: {exc}")
        return 1
    print(decision.rewritten)
    return 0


def _handle_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "rtk_command", None)
    if command == "status":
        return _cmd_status()
    if command == "doctor":
        return _cmd_doctor()
    if command == "install":
        return _cmd_install(getattr(args, "method", "auto"))
    if command == "gain":
        return _cmd_gain(bool(getattr(args, "graph", False)))
    if command == "rewrite":
        return _cmd_rewrite(getattr(args, "command", []))
    print("usage: hermes rtk {status,doctor,install,gain,rewrite}")
    return 2


def register(ctx: Any) -> None:
    """Register RTK's CLI surface and the terminal rewrite hook."""

    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(
            "rtk",
            "Install, inspect, and use RTK command-output compression",
            _setup_cli,
            _handle_cli,
            description=(
                "RTK integration controls and token-savings diagnostics"
            ),
        )

    if _plugin_disabled():
        return

    if not _find_binary():
        global _missing_warned
        if not _missing_warned:
            _warn(
                "RTK is not installed; hook disabled. "
                "Run `hermes rtk install` after enabling this plugin."
            )
            _missing_warned = True
        return

    ctx.register_hook("pre_tool_call", _pre_tool_call)


__all__ = [
    "RewriteResult",
    "register",
    "rewrite_command",
]
