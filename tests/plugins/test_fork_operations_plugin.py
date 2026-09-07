from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "fork-operations"
    / "__init__.py"
)


def _load_plugin():
    name = "hermes_test_fork_operations_plugin"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
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


def test_registers_both_cli_commands():
    plugin = _load_plugin()
    calls = []

    class Context:
        def register_cli_command(self, *args, **kwargs):
            calls.append((args, kwargs))

    plugin.register(Context())

    assert [call[0][0] for call in calls] == ["fork-doctor", "fork-drift"]
    assert all(call[0][2] for call in calls)
    assert all(call[0][3] for call in calls)


def test_collect_drift_parses_left_right_counts(tmp_path, monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_repo_root", lambda _repo=None: tmp_path)
    monkeypatch.setattr(
        plugin,
        "_ensure_upstream_remote",
        lambda _root, repair: (
            "https://github.com/NousResearch/hermes-agent.git",
            "",
        ),
    )
    monkeypatch.setattr(plugin, "_ref_exists", lambda _root, _ref: True)

    def fake_run(args, *, cwd=None, timeout=20.0):
        assert cwd == tmp_path
        if args[:3] == ["rev-list", "--left-right", "--count"]:
            return _completed(list(args), stdout="33\t218\n")
        raise AssertionError(args)

    values = {
        ("rev-parse", "origin/main"): "fork-sha",
        ("rev-parse", "upstream/main"): "upstream-sha",
        ("merge-base", "origin/main", "upstream/main"): "merge-base-sha",
    }
    monkeypatch.setattr(plugin, "_run_git", fake_run)
    monkeypatch.setattr(
        plugin,
        "_git_value",
        lambda _root, args: values.get(tuple(args), ""),
    )

    report = plugin.collect_drift(tmp_path)

    assert report.available is True
    assert report.ahead == 33
    assert report.behind == 218
    assert report.base_sha == "fork-sha"
    assert report.upstream_sha == "upstream-sha"
    assert report.merge_base_sha == "merge-base-sha"


def test_collect_drift_does_not_add_remote_without_explicit_repair(
    tmp_path,
    monkeypatch,
):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_repo_root", lambda _repo=None: tmp_path)
    monkeypatch.setattr(
        plugin,
        "_ensure_upstream_remote",
        lambda _root, repair: ("", "") if not repair else ("unexpected", ""),
    )

    report = plugin.collect_drift(tmp_path, repair_remotes=False)

    assert report.available is False
    assert "missing upstream remote" in report.error


def test_doctor_reports_ready_integrations_without_leaking_secrets(
    tmp_path,
    monkeypatch,
):
    plugin = _load_plugin()
    home = tmp_path / "hermes-home"
    home.mkdir()
    prefix = home / "stable-prefix.txt"
    prefix.write_text("stable prompt", encoding="utf-8")
    (home / "config.yaml").write_text(
        """
plugins:
  enabled:
    - rtk-rewrite
    - cache-foundation
memory:
  provider: knowledge_graph
knowledge_graph:
  uri: bolt://neo4j-host:7687
model:
  provider: lmstudio
  context_length: 65536
terminal:
  backend: ssh
worktree: true
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)
    monkeypatch.setattr(plugin, "_repo_root", lambda _repo=None: tmp_path)
    monkeypatch.setattr(
        plugin,
        "_git_value",
        lambda _root, args: {
            ("branch", "--show-current"): "main",
            ("status", "--porcelain"): "",
        }.get(tuple(args), ""),
    )
    monkeypatch.setattr(
        plugin,
        "_remote_url",
        lambda _root, name: (
            "git@github.com:scottjoyner/hermes-agent.git"
            if name == "origin"
            else "https://github.com/NousResearch/hermes-agent.git"
        ),
    )
    monkeypatch.setattr(
        plugin,
        "_ensure_upstream_remote",
        lambda _root, repair: (
            "https://github.com/NousResearch/hermes-agent.git",
            "",
        ),
    )
    monkeypatch.setattr(plugin, "_ref_exists", lambda _root, _ref: True)
    monkeypatch.setattr(
        plugin,
        "collect_drift",
        lambda *args, **kwargs: plugin.DriftReport(
            available=True,
            base_ref="origin/main",
            upstream_ref="upstream/main",
            ahead=3,
            behind=0,
        ),
    )
    monkeypatch.setattr(plugin.shutil, "which", lambda _name: "/usr/bin/rtk")
    monkeypatch.setenv("NEO4J_PASSWORD", "super-secret-password")
    monkeypatch.setenv("HERMES_CACHE_STABLE_PREFIX_FILE", str(prefix))
    monkeypatch.delenv("HERMES_CACHE_ALLOW_REMOTE", raising=False)

    root, checks, drift = plugin.collect_checks(tmp_path)
    payload = plugin._doctor_payload(root, checks, drift)
    serialized = json.dumps(payload)

    assert payload["summary"]["error"] == 0
    assert "super-secret-password" not in serialized
    by_name = {check.name: check for check in checks}
    assert by_name["plugin.rtk-rewrite"].status == "ok"
    assert by_name["plugin.cache-foundation"].status == "ok"
    assert by_name["memory.provider"].status == "ok"
    assert by_name["memory.neo4j_password"].status == "ok"
    assert by_name["cache.stable_prefix"].status == "ok"
    assert by_name["terminal.isolation"].status == "ok"
    assert by_name["git.worktree_isolation"].status == "ok"


def test_doctor_warns_for_local_unisolated_defaults(tmp_path, monkeypatch):
    plugin = _load_plugin()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\nterminal:\n  backend: local\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(plugin, "get_hermes_home", lambda: home)
    monkeypatch.setattr(plugin, "_repo_root", lambda _repo=None: tmp_path)
    monkeypatch.setattr(
        plugin,
        "_git_value",
        lambda _root, args: {
            ("branch", "--show-current"): "main",
            ("status", "--porcelain"): "",
        }.get(tuple(args), ""),
    )
    monkeypatch.setattr(
        plugin,
        "_remote_url",
        lambda _root, name: (
            "git@github.com:scottjoyner/hermes-agent.git"
            if name == "origin"
            else "https://github.com/NousResearch/hermes-agent.git"
        ),
    )
    monkeypatch.setattr(
        plugin,
        "_ensure_upstream_remote",
        lambda _root, repair: (
            "https://github.com/NousResearch/hermes-agent.git",
            "",
        ),
    )
    monkeypatch.setattr(plugin, "_ref_exists", lambda _root, _ref: True)
    monkeypatch.setattr(
        plugin,
        "collect_drift",
        lambda *args, **kwargs: plugin.DriftReport(
            available=True,
            base_ref="origin/main",
            upstream_ref="upstream/main",
            behind=1,
        ),
    )
    monkeypatch.setattr(plugin.shutil, "which", lambda _name: None)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_CACHE_STABLE_PREFIX_FILE", raising=False)

    _, checks, _ = plugin.collect_checks(tmp_path)
    by_name = {check.name: check for check in checks}

    assert by_name["git.drift"].status == "warning"
    assert by_name["plugin.rtk-rewrite"].status == "warning"
    assert by_name["plugin.cache-foundation"].status == "warning"
    assert by_name["rtk.binary"].status == "warning"
    assert by_name["model.context_length"].status == "warning"
    assert by_name["terminal.isolation"].status == "warning"
    assert by_name["git.worktree_isolation"].status == "warning"
