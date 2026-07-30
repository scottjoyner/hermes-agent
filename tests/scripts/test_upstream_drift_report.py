from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "upstream_drift_report.py"
)


def _load_script():
    name = "hermes_test_upstream_drift_report"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _completed(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_build_report_and_markdown(tmp_path, monkeypatch):
    script = _load_script()
    (tmp_path / ".git").mkdir()

    def fake_run(args, *, cwd, timeout=120.0):
        del timeout
        assert cwd == tmp_path
        command = tuple(args)
        responses = {
            ("remote", "get-url", "upstream"): _completed(
                list(args),
                stdout="https://github.com/NousResearch/hermes-agent.git\n",
            ),
            (
                "rev-list",
                "--left-right",
                "--count",
                "main...upstream/main",
            ): _completed(list(args), stdout="33 218\n"),
            ("rev-parse", "main"): _completed(list(args), stdout="fork-sha\n"),
            ("rev-parse", "upstream/main"): _completed(
                list(args),
                stdout="upstream-sha\n",
            ),
            ("merge-base", "main", "upstream/main"): _completed(
                list(args),
                stdout="merge-base-sha\n",
            ),
        }
        if command not in responses:
            raise AssertionError(command)
        return responses[command]

    monkeypatch.setattr(script, "_run_git", fake_run)

    report = script.build_report(
        tmp_path,
        base_ref="main",
        upstream_ref="upstream/main",
    )
    markdown = script.render_markdown(report)

    assert report.ahead == 33
    assert report.behind == 218
    assert "Action required" in markdown
    assert "| Upstream-only commits | 218 |" in markdown
    assert "cache-foundation" in markdown


def test_existing_unexpected_upstream_remote_fails_closed(tmp_path, monkeypatch):
    script = _load_script()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        script,
        "_run_git",
        lambda args, *, cwd, timeout=120.0: _completed(
            list(args),
            stdout="https://example.com/not-upstream.git\n",
        ),
    )

    try:
        script.build_report(
            tmp_path,
            base_ref="main",
            upstream_ref="upstream/main",
        )
    except RuntimeError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_main_fail_if_behind_returns_one(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setattr(
        script,
        "build_report",
        lambda *args, **kwargs: script.Report(
            repository="/repo",
            base_ref="main",
            upstream_ref="upstream/main",
            ahead=2,
            behind=4,
            base_sha="fork",
            upstream_sha="upstream",
            merge_base_sha="base",
            generated_at="2026-07-29T00:00:00+00:00",
        ),
    )

    result = script.main(["--json", "--fail-if-behind"])

    assert result == 1
    assert '"behind": 4' in capsys.readouterr().out
