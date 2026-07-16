"""Integration tests for the opencode-cli delegation transport.

Two things are verified without spawning a real ``opencode`` binary:

1. ``create_openai_client`` dispatches a provider=="opencode-cli" agent to an
   :class:`agent.opencode_client.OpenCodeClient`.
2. ``delegate_task(..., provider="opencode-cli")`` builds the child with the
   ``opencode-cli`` provider override (and records a delegation in memory).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from agent.opencode_client import OpenCodeClient
from tools import delegate_tool


def _fake_parent():
    parent = mock.MagicMock()
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "x"
    parent.model = "anthropic/claude"
    parent.session_id = "sess-parent"
    parent.cwd = "/tmp"
    parent.terminal_cwd = "/tmp"
    parent._delegate_depth = 0
    parent._memory_manager.on_delegation = mock.MagicMock()
    return parent


def test_create_openai_client_dispatches_opencode_cli():
    from agent import agent_runtime_helpers as arh

    agent = mock.MagicMock()
    agent.provider = "opencode-cli"
    agent.base_url = "opencode://cli"
    agent.api_mode = "chat_completions"
    agent.model = "lmstudio/qwen"
    agent._client_log_context = lambda: "<ctx>"

    client = arh.create_openai_client(
        agent, {"api_key": "x", "base_url": "opencode://cli", "model": "lmstudio/qwen"},
        reason="test", shared=False,
    )
    assert isinstance(client, OpenCodeClient)
    assert client._model == "lmstudio/qwen"


def test_delegate_task_routes_child_to_opencode_cli():
    parent = _fake_parent()

    captured = {}
    real_child = mock.MagicMock()
    real_child.provider = "opencode-cli"
    real_child.api_mode = "chat_completions"
    real_child.session_id = "sess-child"

    def _fake_build(*a, **kw):
        captured["override_provider"] = kw.get("override_provider")
        return real_child

    with mock.patch.object(delegate_tool, "_build_child_agent", side_effect=_fake_build), \
         mock.patch.object(delegate_tool, "_run_single_child",
                           return_value={"summary": "DELEGATED-OK"}):
        result = delegate_tool.delegate_task(
            goal="write a hello world function",
            provider="opencode-cli",
            parent_agent=parent,
        )

    assert captured.get("override_provider") == "opencode-cli"
    assert "DELEGATED-OK" in result
    # The parent's memory manager recorded the delegation with goal/context.
    parent._memory_manager.on_delegation.assert_called_once()
    call_kwargs = parent._memory_manager.on_delegation.call_args.kwargs
    assert call_kwargs["goal"] == "write a hello world function"
    assert call_kwargs["child_session_id"] == "sess-child"
