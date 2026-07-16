"""OpenCode subagent transport for Hermes delegation.

This lets a Hermes agent delegate a sub-task to a *real* ``opencode`` CLI
session (rather than a sibling Hermes AIAgent).  ``opencode run`` resolves
its own tools, sandboxing and model internally, so from Hermes' point of view
the child is a pure text-in / text-out solver.

Design mirrors :mod:`agent.copilot_acp_client`:

* Expose ``client.chat.completions.create(**kwargs)`` so the existing
  ``chat_completion_helpers`` machinery works unchanged.
* Spawn ``opencode run --prompt <...> --auto --model <provider/model>`` and
  capture stdout as the final assistant answer.
* Return an ``openai``-shaped ``SimpleNamespace`` (``choices[0].message``).

opencode never emits OpenAI tool-call wire shapes back to us — delegation is
synchronous and one-shot, so the child's tool use is internal to its session.
The result therefore always comes back as plain text with
``finish_reason == "stop"``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from typing import Any

from types import SimpleNamespace


_DEFAULT_TIMEOUT_SECONDS = 1800.0
_OPENCODE_MARKER_BASE_URL = "opencode://cli"


def _resolve_command() -> str:
    found = shutil.which("opencode")
    if found:
        return found
    # Common install locations.
    import os

    for cand in (
        os.path.expanduser("~/.opencode/bin/opencode"),
        "/home/scott/.opencode/bin/opencode",
        "/usr/local/bin/opencode",
    ):
        if os.path.exists(cand):
            return cand
    return "opencode"


class OpenCodeClient:
    """A thin OpenAI-compatible client that shells out to ``opencode run``."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        timeout: float | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "opencode-cli"
        self.base_url = base_url or _OPENCODE_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._command = command or _resolve_command()
        self._extra_args = list(args or [])
        self._cwd = cwd or None
        self._model = model
        self._agent = agent
        self._timeout = float(timeout or _DEFAULT_TIMEOUT_SECONDS)
        self.is_closed = False
        self.chat = _OpenCodeChatNamespace(self)

    def close(self) -> None:
        self.is_closed = True

    # -- core ----------------------------------------------------------------

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **_: Any,
    ) -> Any:
        prompt_text = _format_messages_as_prompt(messages or [])
        if timeout is None:
            effective_timeout = self._timeout
        elif isinstance(timeout, (int, float)):
            effective_timeout = float(timeout)
        else:
            # httpx.Timeout object (used natively by the OpenAI SDK).
            candidates = [
                getattr(timeout, a, None)
                for a in ("read", "write", "connect", "pool", "timeout")
            ]
            numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
            effective_timeout = max(numeric) if numeric else self._timeout

        response_text = self._run_prompt(prompt_text, timeout_seconds=effective_timeout)

        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )

        if stream:
            # The parent always streams; yield the full answer as a single
            # delta chunk followed by a terminal chunk carrying finish_reason
            # and usage.  Mirrors the OpenAI streaming wire shape.
            model_name = model or self._model or "opencode-cli"

            def _chunks():
                if response_text:
                    yield SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                index=0,
                                delta=SimpleNamespace(
                                    role="assistant", content=response_text,
                                    tool_calls=None, reasoning=None,
                                ),
                                finish_reason=None,
                            )
                        ],
                        usage=None,
                        model=model_name,
                    )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            index=0,
                            delta=SimpleNamespace(
                                role=None, content="", tool_calls=None,
                                reasoning=None,
                            ),
                            finish_reason="stop",
                        )
                    ],
                    usage=usage,
                    model=model_name,
                )

            return _chunks()

        assistant_message = SimpleNamespace(
            content=response_text,
            tool_calls=[],
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        choice = SimpleNamespace(message=assistant_message, finish_reason="stop")
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model or self._model or "opencode-cli",
        )

    def _run_prompt(self, prompt_text: str, *, timeout_seconds: float) -> str:
        # opencode's `run` takes the message positionally (the `--prompt` flag
        # is not honoured in all builds).  `--auto` auto-approves permissions.
        cmd = [self._command, "run", prompt_text, "--auto", "--format", "default"]
        if self._model or self._model_hint():
            cmd += ["--model", self._model or self._model_hint()]
        if self._agent:
            cmd += ["--agent", self._agent]
        cmd += self._extra_args

        try:
            proc = subprocess.run(
                cmd,
                cwd=self._cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing
            raise RuntimeError(
                f"opencode run timed out after {timeout_seconds:.0f}s"
            ) from exc

        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            raise RuntimeError(
                f"opencode run exited {proc.returncode}: {err[:500]}"
            )

        return _clean_response(proc.stdout or "")

    def _model_hint(self) -> str | None:
        import os

        return os.getenv("OPENCODE_CLI_MODEL") or None


class _OpenCodeChatCompletions:
    def __init__(self, client: "OpenCodeClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _OpenCodeChatNamespace:
    def __init__(self, client: "OpenCodeClient"):
        self.completions = _OpenCodeChatCompletions(client)


def _format_messages_as_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten a Hermes/OpenAI message list into a single prompt string."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        name = msg.get("name")
        content = msg.get("content")
        if content is None:
            # tool / assistant-with-tool-calls messages: serialise meaningfully.
            chunks = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                chunks.append(f"[{role} called {fn.get('name','')}({fn.get('arguments','')})]")
            content = " ".join(chunks)
        if not content:
            continue
        prefix = {
            "system": "System:",
            "user": "User:",
            "assistant": "Assistant:",
            "tool": "Tool result:",
        }.get(role, f"{role.capitalize()}:")
        line = f"{prefix} {content}" if not name else f"{prefix} ({name}): {content}"
        parts.append(line)
    return "\n\n".join(parts)


# opencode prints a trailing TUI/ASCII banner and prompt boilerplate; strip it.
_BANNER_RE = re.compile(r"(\x1b\[[0-9;]*[A-Za-z])")
_TRAILING_PROMPT_RE = re.compile(r"\n?>\s*$")
_FOOTER_RE = re.compile(r"^\s*>\s*.*\S.*$", re.MULTILINE)  # e.g. "> build · tencent/hy3:free"


def _clean_response(text: str) -> str:
    text = _BANNER_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    # Drop the trailing prompt/footer lines ("> build · model").
    text = _FOOTER_RE.sub("", text)
    text = text.strip()
    text = _TRAILING_PROMPT_RE.sub("", text)
    return text.strip()
