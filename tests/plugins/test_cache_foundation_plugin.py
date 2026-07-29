from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "cache-foundation"
    / "__init__.py"
)


def _load_plugin():
    name = "hermes_test_cache_foundation_plugin"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CACHE_STATE_DB", str(tmp_path / "cache.db"))
    monkeypatch.delenv("HERMES_CACHE_DISABLE", raising=False)
    monkeypatch.delenv("HERMES_CACHE_ALLOW_REMOTE", raising=False)
    module = _load_plugin()
    module._reset_store_for_tests()
    return module


def _kwargs(*, request=None, base_url="http://localhost:1234/v1"):
    return {
        "request": request
        or {
            "model": "local-model",
            "messages": [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "hello"},
            ],
        },
        "session_id": "session-1",
        "model": "local-model",
        "provider": "lmstudio",
        "base_url": base_url,
        "api_mode": "chat_completions",
        "api_request_id": "request-1",
    }


def test_registers_cli_and_both_llm_middleware(plugin):
    calls = []

    class Context:
        def register_cli_command(self, *args, **kwargs):
            calls.append(("cli", args, kwargs))

        def register_middleware(self, kind, callback):
            calls.append((kind, callback, {}))

    plugin.register(Context())

    assert calls[0][0] == "cli"
    assert {calls[1][0], calls[2][0]} == {"llm_request", "llm_execution"}


def test_request_middleware_adds_local_headers_and_preserves_existing(plugin):
    kwargs = _kwargs()
    kwargs["request"]["extra_headers"] = {"X-Existing": "yes"}

    result = plugin._cache_request(**kwargs)

    assert result is not None
    request = result["request"]
    assert request["extra_headers"]["X-Existing"] == "yes"
    assert request["extra_headers"]["X-Hermes-Cache-Session-Id"] == "session-1"
    assert "X-Hermes-Cache-Checkpoint-Id" in request["extra_headers"]
    assert "system prompt" not in str(request["extra_headers"])
    assert plugin._store().affinity("session-1") is not None


def test_request_middleware_ignores_remote_endpoint_by_default(plugin):
    result = plugin._cache_request(
        **_kwargs(base_url="https://api.example.com/v1")
    )

    assert result is None
    assert plugin._store().affinity("session-1") is None


def test_request_middleware_can_opt_in_trusted_remote(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_CACHE_ALLOW_REMOTE", "1")

    result = plugin._cache_request(
        **_kwargs(base_url="https://cache-proxy.example.com/v1")
    )

    assert result is not None
    assert result["request"]["extra_headers"]["X-Hermes-Cache-Mode"] == "prefer"


def test_non_chat_api_modes_are_untouched(plugin):
    kwargs = _kwargs()
    kwargs["api_mode"] = "anthropic_messages"

    assert plugin._cache_request(**kwargs) is None


def test_execution_records_cache_metrics_and_calls_provider_once(plugin):
    kwargs = _kwargs()
    prepared = plugin._cache_request(**kwargs)
    assert prepared is not None
    calls = []

    def next_call(request):
        calls.append(request)
        return SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=500,
                completion_tokens=25,
                prompt_tokens_details=SimpleNamespace(cached_tokens=320),
            )
        )

    response = plugin._cache_execution(
        **{**kwargs, "request": prepared["request"], "next_call": next_call}
    )

    assert response.usage.prompt_tokens == 500
    assert len(calls) == 1
    recorded = plugin._store().recent_requests(session_id="session-1")
    assert len(recorded) == 1
    assert recorded[0]["success"] == 1
    assert recorded[0]["cache_read_tokens"] == 320
    assert plugin._store().checkpoints()[0]["state"] == "hit"


def test_execution_records_error_and_does_not_retry(plugin):
    kwargs = _kwargs()
    prepared = plugin._cache_request(**kwargs)
    assert prepared is not None
    calls = []

    def next_call(request):
        calls.append(request)
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        plugin._cache_execution(
            **{**kwargs, "request": prepared["request"], "next_call": next_call}
        )

    assert len(calls) == 1
    recorded = plugin._store().recent_requests(session_id="session-1")
    assert recorded[0]["success"] == 0
    assert "provider failed" in recorded[0]["error"]


def test_usage_metrics_support_anthropic_cache_fields(plugin):
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=700,
            output_tokens=30,
            cache_read_input_tokens=500,
            cache_creation_input_tokens=100,
        )
    )

    assert plugin._usage_metrics(response) == {
        "prompt_tokens": 700,
        "completion_tokens": 30,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
    }


def test_disabled_plugin_keeps_cli_but_skips_middleware(plugin, monkeypatch):
    monkeypatch.setenv("HERMES_CACHE_DISABLE", "1")
    calls = []

    class Context:
        def register_cli_command(self, *args, **kwargs):
            calls.append("cli")

        def register_middleware(self, kind, callback):
            calls.append(kind)

    plugin.register(Context())

    assert calls == ["cli"]


def test_warm_refuses_remote_endpoint_without_explicit_opt_in(
    plugin,
    tmp_path,
    capsys,
):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("system prompt", encoding="utf-8")

    result = plugin._cmd_warm(
        "https://api.example.com/v1",
        "model",
        str(prompt_file),
        1.0,
    )

    assert result == 2
    assert "Refusing" in capsys.readouterr().err
