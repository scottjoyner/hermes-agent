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
    name = "hermes_test_upstream_drift_redaction"
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


def test_unexpected_remote_error_redacts_embedded_credentials(
    tmp_path,
    monkeypatch,
):
    script = _load_script()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        script,
        "_run_git",
        lambda args, *, cwd, timeout=120.0: _completed(
            list(args),
            stdout=(
                "https://token:secret@example.com/not-upstream.git?key=value\n"
            ),
        ),
    )

    try:
        script.build_report(
            tmp_path,
            base_ref="main",
            upstream_ref="upstream/main",
        )
    except RuntimeError as exc:
        message = str(exc)
        assert "example.com/not-upstream.git" in message
        assert "token" not in message
        assert "secret" not in message
        assert "key=value" not in message
    else:
        raise AssertionError("expected RuntimeError")
