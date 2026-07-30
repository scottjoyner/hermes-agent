from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "small-model-context"
    / "__init__.py"
)


def _load_plugin():
    name = "hermes_test_small_model_context_plugin"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _skills_prompt() -> str:
    return """Identity and safety guidance.

## Skills (mandatory)
Before replying, scan every skill and load anything relevant. This deliberately
verbose paragraph represents the upstream always-on instructions.

<available_skills>
  coding: Software development workflows
    - github-review: Inspect pull requests, unresolved comments, and repository state before changing code.
    - python-testing: Run focused pytest tests and expand to broader validation only after the focused path passes.
  travel: Planning and booking
    - trip-planning: Research destinations, compare routes, and assemble detailed itineraries.
</available_skills>

Only proceed without loading a skill if none are relevant.

Project context follows.
"""


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": (
                    "Search files in the active workspace using a bounded query. "
                    "This long explanation includes examples, caveats, and workflow "
                    "advice that are useful to a large model but expensive to resend."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "The exact search text. Prefer a symbol, error message, "
                                "or distinctive phrase and avoid broad generic words."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results to return.",
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["query"],
                },
            },
        }
    ]


def test_auto_profile_uses_real_context_window():
    plugin = _load_plugin()

    lean = plugin.resolve_policy(
        config={"model": {"context_length": 16_384}},
        model="qwen3-8b",
    )
    balanced = plugin.resolve_policy(
        config={"model": {"context_length": 65_536}},
        model="qwen3-8b",
    )
    full = plugin.resolve_policy(
        config={"model": {"context_length": 262_144}},
        model="qwen3-8b",
    )

    assert lean.profile == "lean"
    assert lean.skill_mode == "names"
    assert balanced.profile == "balanced"
    assert balanced.skill_mode == "compact"
    assert full.profile == "full"
    assert full.skill_mode == "full"


def test_explicit_profile_overrides_model_name_and_window():
    plugin = _load_plugin()
    policy = plugin.resolve_policy(
        config={
            "model": {"context_length": 8_192},
            "agent": {"context_profile": "full"},
        },
        model="tiny-1b",
    )

    assert policy.profile == "full"
    assert policy.enabled is False
    assert policy.source == "config"


def test_model_size_fallback_when_window_is_unknown():
    plugin = _load_plugin()

    assert plugin.resolve_policy(config={}, model="qwen3-8b").profile == "lean"
    assert plugin.resolve_policy(config={}, model="qwen3-35b-a3b").profile == "balanced"
    assert plugin.resolve_policy(config={}, model="llama-3.1-405b").profile == "full"


def test_names_mode_keeps_every_skill_and_removes_descriptions():
    plugin = _load_plugin()
    policy = plugin.resolve_policy(
        config={"agent": {"context_profile": "lean"}},
    )

    compacted = plugin.compact_skills_prompt(_skills_prompt(), policy)

    assert "github-review" in compacted
    assert "python-testing" in compacted
    assert "trip-planning" in compacted
    assert "coding: github-review, python-testing" in compacted
    assert "travel: trip-planning" in compacted
    assert "Inspect pull requests" not in compacted
    assert "Research destinations" not in compacted
    assert "Project context follows." in compacted


def test_balanced_mode_bounds_skill_descriptions():
    plugin = _load_plugin()
    policy = plugin.resolve_policy(
        config={
            "agent": {"context_profile": "balanced"},
            "context_optimizer": {"skill_description_chars": 42},
        },
    )

    compacted = plugin.compact_skills_prompt(_skills_prompt(), policy)

    assert "github-review:" in compacted
    assert "python-testing:" in compacted
    assert "…" in compacted
    available = compacted.split("<available_skills>", 1)[1].split(
        "</available_skills>", 1
    )[0]
    for line in available.splitlines():
        if line.strip().startswith("-") and ":" in line:
            description = line.split(":", 1)[1].strip()
            assert len(description) <= 43


def test_tool_compaction_preserves_schema_contract():
    plugin = _load_plugin()
    policy = plugin.resolve_policy(
        config={
            "agent": {"context_profile": "lean"},
            "context_optimizer": {
                "tool_description_chars": 72,
                "parameter_description_chars": 38,
            },
        },
    )

    original = _tools()
    compacted = plugin.compact_tools(original, policy)
    function = compacted[0]["function"]
    parameters = function["parameters"]

    assert compacted is not original
    assert function["name"] == "search_files"
    assert parameters["required"] == ["query"]
    assert parameters["properties"]["limit"]["minimum"] == 1
    assert parameters["properties"]["limit"]["maximum"] == 100
    assert len(function["description"]) <= 73
    assert len(parameters["properties"]["query"]["description"]) <= 39
    assert original[0]["function"]["description"].startswith("Search files")
    assert len(original[0]["function"]["description"]) > 72


def test_optimize_request_does_not_mutate_original():
    plugin = _load_plugin()
    policy = plugin.resolve_policy(
        config={"agent": {"context_profile": "lean"}},
    )
    request = {
        "model": "qwen3-8b",
        "messages": [
            {"role": "system", "content": _skills_prompt()},
            {"role": "user", "content": "Fix the test."},
        ],
        "tools": _tools(),
    }
    original_json = json.dumps(request, sort_keys=True)

    updated, telemetry = plugin.optimize_request(request, policy)

    assert json.dumps(request, sort_keys=True) == original_json
    assert telemetry["changed"] == 1
    assert telemetry["after_chars"] < telemetry["before_chars"]
    assert telemetry["saved_chars"] > 0
    assert updated["messages"][1] == request["messages"][1]


def test_middleware_policy_is_stable_for_session(monkeypatch):
    plugin = _load_plugin()
    plugin._reset_policy_cache_for_tests()
    policies = [
        plugin.resolve_policy(config={"agent": {"context_profile": "lean"}}),
        plugin.resolve_policy(config={"agent": {"context_profile": "full"}}),
    ]
    calls = []

    def fake_resolve_policy(**kwargs):
        calls.append(kwargs)
        return policies[min(len(calls) - 1, 1)]

    monkeypatch.setattr(plugin, "resolve_policy", fake_resolve_policy)
    kwargs = {
        "session_id": "stable-session",
        "model": "local-model",
        "request": {
            "messages": [{"role": "system", "content": _skills_prompt()}],
            "tools": _tools(),
        },
    }

    first = plugin._optimize_request(**kwargs)
    second = plugin._optimize_request(**kwargs)

    assert first is not None
    assert second is not None
    assert len(calls) == 1
    assert "applied lean context profile" in first["reason"]


def test_full_profile_returns_passthrough(monkeypatch):
    plugin = _load_plugin()
    plugin._reset_policy_cache_for_tests()
    monkeypatch.setattr(
        plugin,
        "resolve_policy",
        lambda **kwargs: plugin.ContextPolicy(
            profile="full",
            context_length=262_144,
            skill_mode="full",
            tool_mode="full",
            tool_description_chars=0,
            parameter_description_chars=0,
            skill_description_chars=0,
        ),
    )

    result = plugin._optimize_request(
        session_id="full-session",
        model="large-model",
        request={
            "messages": [{"role": "system", "content": _skills_prompt()}],
            "tools": _tools(),
        },
    )

    assert result is None


def test_registers_cli_and_request_middleware():
    plugin = _load_plugin()
    cli_calls = []
    middleware_calls = []

    class Context:
        def register_cli_command(self, *args, **kwargs):
            cli_calls.append((args, kwargs))

        def register_middleware(self, *args, **kwargs):
            middleware_calls.append((args, kwargs))

    plugin.register(Context())

    assert cli_calls[0][0][0] == "context-opt"
    assert middleware_calls[0][0][0] == "llm_request"
    assert middleware_calls[0][0][1] is plugin._optimize_request
