"""OpenCode CLI subagent provider profile.

This is a *transport* profile: it does not hit a network API.  When a Hermes
agent delegates a sub-task to ``provider="opencode-cli"`` (see
``tools/delegate_tool.py``), ``agent/agent_runtime_helpers.py`` builds an
:class:`agent.opencode_client.OpenCodeClient` that shells out to the real
``opencode`` binary.  OpenCode resolves its own model/tools/sandboxing.

The model string here is only a hint passed through to ``opencode run --model``;
if unset, opencode uses its own configured default.
"""

from __future__ import annotations

from providers import register_provider
from providers.base import ProviderProfile


class OpenCodeCliProfile(ProviderProfile):
    """Routes to the local ``opencode`` CLI as a delegation backend."""

    def __init__(self) -> None:
        super().__init__(
            name="opencode-cli",
            api_mode="chat_completions",
            display_name="OpenCode CLI",
            description="Delegate sub-tasks to the local opencode CLI as a real agent",
            base_url="opencode://cli",
            env_vars=(),
            auth_type="api_key",
            supports_health_check=False,
            fallback_models=(),
        )


register_provider(OpenCodeCliProfile())
