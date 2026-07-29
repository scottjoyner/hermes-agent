from __future__ import annotations

import argparse
import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from unittest import mock


PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "rtk-rewrite"
    / "__init__.py"
)


class FakeContext:
    def __init__(self) -> None:
        self.hooks = {}
        self.cli_commands = {}

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback

    def register_cli_command(
        self,
        name,
        help_text,
        setup_fn,
        handler_fn=None,
        description="",
    ) -> None:
        self.cli_commands[name] = {
            "help": help_text,
            "setup": setup_fn,
            "handler": handler_fn,
            "description": description,
        }


def load_plugin():
    module_name = "test_rtk_rewrite_plugin_module"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module._binary_checked = False
    module._cached_binary = None
    module._missing_warned = False
    return module


def completed(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["rtk"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_register_exposes_cli_but_skips_hook_when_rtk_missing():
    module = load_plugin()
    ctx = FakeContext()

    with (
        mock.patch.object(module.shutil, "which", return_value=None),
        mock.patch.object(module.sys, "stderr", new_callable=io.StringIO) as stderr,
    ):
        module.register(ctx)

    assert "rtk" in ctx.cli_commands
    assert "pre_tool_call" not in ctx.hooks
    assert "RTK is not installed" in stderr.getvalue()


def test_register_adds_pre_tool_hook_when_rtk_is_available():
    module = load_plugin()
    ctx = FakeContext()

    with mock.patch.object(
        module.shutil,
        "which",
        return_value="/usr/local/bin/rtk",
    ):
        module.register(ctx)

    assert "rtk" in ctx.cli_commands
    assert "pre_tool_call" in ctx.hooks


def test_disabled_plugin_keeps_cli_and_skips_hook(monkeypatch):
    module = load_plugin()
    ctx = FakeContext()
    monkeypatch.setenv("HERMES_RTK_DISABLE", "true")

    module.register(ctx)

    assert "rtk" in ctx.cli_commands
    assert "pre_tool_call" not in ctx.hooks


def test_rewrite_success_mutates_terminal_command():
    module = load_plugin()
    args = {"command": "git status"}

    with mock.patch.object(
        module,
        "_run_rtk",
        return_value=completed(stdout="rtk git status\n"),
    ):
        module._pre_tool_call(tool_name="terminal", args=args)

    assert args == {"command": "rtk git status"}


def test_rewrite_returncode_three_is_accepted():
    module = load_plugin()
    args = {"command": "pytest"}

    with mock.patch.object(
        module,
        "_run_rtk",
        return_value=completed(
            returncode=3,
            stdout="rtk pytest\n",
        ),
    ):
        module._pre_tool_call(tool_name="terminal", args=args)

    assert args == {"command": "rtk pytest"}


def test_passthrough_return_codes_preserve_original_command():
    for returncode in (1, 2):
        module = load_plugin()
        args = {"command": "echo hello"}

        with mock.patch.object(
            module,
            "_run_rtk",
            return_value=completed(
                returncode=returncode,
                stdout="rtk echo hello\n",
            ),
        ):
            module._pre_tool_call(tool_name="terminal", args=args)

        assert args == {"command": "echo hello"}


def test_unexpected_exit_fails_open_and_warns():
    module = load_plugin()
    args = {"command": "git status"}

    with (
        mock.patch.object(
            module,
            "_run_rtk",
            return_value=completed(returncode=9, stderr="bad rewrite"),
        ),
        mock.patch.object(module.sys, "stderr", new_callable=io.StringIO) as stderr,
    ):
        module._pre_tool_call(tool_name="terminal", args=args)

    assert args == {"command": "git status"}
    assert "exit 9" in stderr.getvalue()
    assert "bad rewrite" in stderr.getvalue()


def test_timeout_fails_open_and_warns():
    module = load_plugin()
    args = {"command": "git status"}
    timeout = subprocess.TimeoutExpired(
        cmd=["rtk", "rewrite", "git status"],
        timeout=2,
    )

    with (
        mock.patch.object(module, "_run_rtk", side_effect=timeout),
        mock.patch.object(module.sys, "stderr", new_callable=io.StringIO) as stderr,
    ):
        module._pre_tool_call(tool_name="terminal", args=args)

    assert args == {"command": "git status"}
    assert "timed out" in stderr.getvalue()


def test_non_terminal_and_invalid_payloads_do_not_call_rtk():
    module = load_plugin()

    with mock.patch.object(module, "_run_rtk") as run:
        module._pre_tool_call(
            tool_name="read_file",
            args={"command": "git status"},
        )
        module._pre_tool_call(tool_name="terminal", args={})
        module._pre_tool_call(
            tool_name="terminal",
            args={"command": ["git", "status"]},
        )
        module._pre_tool_call(
            tool_name="terminal",
            args={"command": "   "},
        )

    run.assert_not_called()


def test_timeout_configuration_is_bounded(monkeypatch):
    module = load_plugin()

    monkeypatch.setenv("HERMES_RTK_REWRITE_TIMEOUT", "0")
    assert module._timeout_seconds() == 0.1

    monkeypatch.setenv("HERMES_RTK_REWRITE_TIMEOUT", "500")
    assert module._timeout_seconds() == 10.0

    monkeypatch.setenv("HERMES_RTK_REWRITE_TIMEOUT", "invalid")
    assert module._timeout_seconds() == 2.0


def test_cli_parser_registers_all_commands():
    module = load_plugin()
    parser = argparse.ArgumentParser(prog="hermes rtk")

    module._setup_cli(parser)

    for command in ("status", "doctor", "install", "gain"):
        args = parser.parse_args([command])
        assert args.func is module._handle_cli
        assert args.rtk_command == command

    args = parser.parse_args(["rewrite", "git", "status"])
    assert args.command == ["git", "status"]


def test_status_reports_binary_and_rewrite_state(capsys):
    module = load_plugin()

    with (
        mock.patch.object(
            module,
            "_version_line",
            return_value=(True, "rtk 0.28.2"),
        ),
        mock.patch.object(
            module,
            "_find_binary",
            return_value="/usr/local/bin/rtk",
        ),
    ):
        assert module._cmd_status() == 0

    output = capsys.readouterr().out
    assert '"available": true' in output
    assert '"rewrite_enabled": true' in output
    assert "/usr/local/bin/rtk" in output


def test_install_auto_prefers_brew_then_cargo():
    module = load_plugin()

    with mock.patch.object(
        module.shutil,
        "which",
        side_effect=lambda name: (
            "/opt/homebrew/bin/brew" if name == "brew" else None
        ),
    ):
        assert module._install_command("auto") == [
            "brew",
            "install",
            "rtk",
        ]

    with mock.patch.object(
        module.shutil,
        "which",
        side_effect=lambda name: (
            "/usr/bin/cargo" if name == "cargo" else None
        ),
    ):
        assert module._install_command("auto") == [
            "cargo",
            "install",
            "--git",
            "https://github.com/rtk-ai/rtk",
        ]


def test_rewrite_cli_prints_preview(capsys):
    module = load_plugin()
    decision = module.RewriteResult(
        original="git status",
        rewritten="rtk git status",
        changed=True,
        returncode=0,
    )

    with mock.patch.object(
        module,
        "rewrite_command",
        return_value=decision,
    ):
        assert module._cmd_rewrite(["git status"]) == 0

    assert capsys.readouterr().out.strip() == "rtk git status"
