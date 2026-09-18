"""Shadow-only my-jev policy observation for Hermes user turns.

This module must never affect the live conversation/tool path.  It builds a
bounded policy-state snapshot, asks the optional local my-jev sidecar for a
typed policy decision, and appends the evidence to a local JSONL file for later
AssistX/my-jev evaluation.

No shadow response is injected into model context or used to authorize tools.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from agent.message_content import flatten_message_text
from hermes_constants import get_hermes_home
from utils import env_var_enabled

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8088/v1/agent-policy"
_MAX_HISTORY_MESSAGES = 8
_MAX_MESSAGE_CHARS = 1000
_MAX_HISTORY_CHARS = 6000


def shadow_enabled() -> bool:
    return env_var_enabled(
        "HERMES_MY_JEV_POLICY_SHADOW_ENABLED"
    )


def _env_bool(
    name: str,
    default: bool = False,
) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }


def _session_platform() -> str:
    try:
        from gateway.session_context import get_session_env

        return (
            get_session_env(
                "HERMES_SESSION_PLATFORM",
                "",
            )
            or "cli"
        )
    except Exception:
        return (
            os.getenv(
                "HERMES_SESSION_PLATFORM",
                "",
            )
            or "cli"
        )


def _recent_context(
    conversation_history: list[dict[str, Any]] | None,
) -> str:
    if not conversation_history:
        return ""

    chunks: list[str] = []
    for message in conversation_history[
        -_MAX_HISTORY_MESSAGES:
    ]:
        if not isinstance(message, dict):
            continue
        role = str(
            message.get("role") or "unknown"
        )
        text = flatten_message_text(
            message.get("content")
        ).strip()
        if not text:
            continue
        chunks.append(
            f"{role}: "
            f"{text[:_MAX_MESSAGE_CHARS]}"
        )

    summary = "\n".join(chunks)
    return summary[-_MAX_HISTORY_CHARS:]


def _tool_names(agent: Any) -> list[str]:
    names: set[str] = set()
    for tool in getattr(
        agent,
        "tools",
        None,
    ) or []:
        if isinstance(tool, dict):
            direct = tool.get("name")
            if direct:
                names.add(str(direct))
            function = tool.get("function")
            if isinstance(function, dict):
                name = function.get("name")
                if name:
                    names.add(str(name))
            continue

        name = getattr(tool, "name", None)
        if name:
            names.add(str(name))
            continue
        function = getattr(
            tool,
            "function",
            None,
        )
        function_name = getattr(
            function,
            "name",
            None,
        )
        if function_name:
            names.add(
                str(function_name)
            )
    return sorted(names)


def build_policy_request(
    agent: Any,
    user_message: Any,
    conversation_history: list[dict[str, Any]] | None,
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    utterance = flatten_message_text(
        user_message
    ).strip()
    session_id = str(
        getattr(
            agent,
            "session_id",
            "",
        )
        or ""
    )
    speaker_verified = _env_bool(
        "HERMES_MY_JEV_SPEAKER_VERIFIED",
        False,
    )
    actions_allowed = _env_bool(
        "HERMES_MY_JEV_ACTIONS_ALLOWED",
        False,
    )
    external_allowed = _env_bool(
        "HERMES_MY_JEV_EXTERNAL_ACTIONS_ALLOWED",
        False,
    )
    privileged_allowed = _env_bool(
        "HERMES_MY_JEV_PRIVILEGED_ACTIONS_ALLOWED",
        False,
    )

    state = {
        "utterance": utterance,
        "conversation_summary": (
            _recent_context(
                conversation_history
            )
        ),
        "source": _session_platform(),
        "speaker_id": os.getenv(
            "HERMES_MY_JEV_SPEAKER_ID",
            "",
        ),
        "speaker_verified": speaker_verified,
        "foreground": not env_var_enabled(
            "HERMES_CRON_SESSION"
        ),
        "active_work": (
            [str(task_id)]
            if task_id
            else []
        ),
        "pending_approvals": [],
        "available_capabilities": [
            "chat",
            "task_graph",
            "tools",
            "delegate",
        ],
        "available_tools": _tool_names(
            agent
        ),
        "actions_allowed": actions_allowed,
        "external_actions_allowed": (
            external_allowed
        ),
        "privileged_actions_allowed": (
            privileged_allowed
        ),
        "metadata": {
            "hermes_session_id": session_id,
            "task_id": str(
                task_id or ""
            ),
            "shadow_source": (
                "hermes_pre_turn"
            ),
        },
    }
    constraints = {
        "speaker_verified": (
            speaker_verified
        ),
        "actions_allowed": (
            actions_allowed
        ),
        "local_writes_allowed": _env_bool(
            "HERMES_MY_JEV_LOCAL_WRITES_ALLOWED",
            actions_allowed,
        ),
        "external_actions_allowed": (
            external_allowed
        ),
        "privileged_actions_allowed": (
            privileged_allowed
        ),
        "approval_gate_available": _env_bool(
            "HERMES_MY_JEV_APPROVAL_GATE_AVAILABLE",
            True,
        ),
        "active_work": bool(task_id),
    }
    return {
        "state": state,
        "constraints": constraints,
    }


def _evidence_path(
    session_id: str,
) -> Path:
    root = (
        get_hermes_home()
        / "policy_shadow"
    )
    root.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )
    try:
        root.chmod(0o700)
    except OSError:
        pass
    safe_session = "".join(
        character
        if character.isalnum()
        or character in {"-", "_"}
        else "_"
        for character in (
            session_id or "default"
        )
    )[:120]
    return root / (
        f"{safe_session}.jsonl"
    )


def _append_evidence(
    session_id: str,
    evidence: dict[str, Any],
) -> None:
    path = _evidence_path(
        session_id
    )
    with path.open(
        "a",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                evidence,
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def observe_policy_shadow(
    agent: Any,
    user_message: Any,
    conversation_history: list[dict[str, Any]] | None,
    *,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """Observe one real user turn and fail open on every shadow-path error."""
    if not shadow_enabled():
        return None

    try:
        request_payload = (
            build_policy_request(
                agent,
                user_message,
                conversation_history,
                task_id=task_id,
            )
        )
        if not request_payload[
            "state"
        ]["utterance"]:
            return None

        timeout = max(
            0.05,
            float(
                os.getenv(
                    "HERMES_MY_JEV_POLICY_TIMEOUT_S",
                    "0.50",
                )
            ),
        )
        url = os.getenv(
            "HERMES_MY_JEV_POLICY_URL",
            _DEFAULT_URL,
        ).strip()

        response = requests.post(
            url,
            json=request_payload,
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(
                "policy response is not a JSON object"
            )
        if (
            body.get("contract")
            != "assistx-agent-policy-v1"
        ):
            raise ValueError(
                "unexpected my-jev policy contract"
            )

        session_id = str(
            getattr(
                agent,
                "session_id",
                "",
            )
            or ""
        )
        evidence = {
            "shadow": True,
            "recorded_at_ts": time.time(),
            "source": "hermes_pre_turn",
            "session_id": session_id,
            "task_id": str(
                task_id or ""
            ),
            "request": request_payload,
            "response": body,
            # Raw local trajectory evidence has not passed a redaction pipeline.
            "redacted": False,
        }
        _append_evidence(
            session_id,
            evidence,
        )
        return evidence
    except Exception as exc:
        logger.warning(
            "my-jev policy shadow observation failed: %s",
            exc,
        )
        return None
