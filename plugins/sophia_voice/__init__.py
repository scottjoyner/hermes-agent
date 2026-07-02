"""Sophia voice sidecar tools for Hermes."""

from __future__ import annotations

import os
from typing import Any, Dict

import requests

from tools.registry import tool_error, tool_result


DEFAULT_BASE_URL = "http://127.0.0.1:8765"


def _base_url() -> str:
    return os.getenv("SOPHIA_VOICE_URL", DEFAULT_BASE_URL).rstrip("/")


def _request(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    url = f"{_base_url()}{path}"
    resp = requests.request(method, url, timeout=kwargs.pop("timeout", 20), **kwargs)
    resp.raise_for_status()
    return resp.json()


def _handle_status(args: dict, **_kw: Any) -> str:
    try:
        return tool_result(_request("GET", "/status"))
    except Exception as exc:
        return tool_error(f"Sophia voice sidecar is unavailable at {_base_url()}: {exc}")


def _handle_intent(args: dict, **_kw: Any) -> str:
    transcript = str(args.get("transcript") or "").strip()
    if not transcript:
        return tool_error("transcript is required")
    try:
        return tool_result(_request("POST", "/intent", json={"transcript": transcript}))
    except Exception as exc:
        return tool_error(f"Sophia intent detection failed: {exc}")


def _handle_chat(args: dict, **_kw: Any) -> str:
    transcript = str(args.get("transcript") or "").strip()
    if not transcript:
        return tool_error("transcript is required")
    payload = {
        "transcript": transcript,
        "session_id": str(args.get("session_id") or "hermes-tool"),
        "user_id": str(args.get("user_id") or "default"),
    }
    try:
        return tool_result(_request("POST", "/voice-chat", json=payload, timeout=60))
    except Exception as exc:
        return tool_error(f"Sophia voice chat failed: {exc}")


def _handle_events(args: dict, **_kw: Any) -> str:
    params: Dict[str, Any] = {}
    if args.get("after_id") is not None:
        params["after_id"] = args.get("after_id")
    if args.get("session_id"):
        params["session_id"] = args.get("session_id")
    try:
        return tool_result(_request("GET", "/events", params=params))
    except Exception as exc:
        return tool_error(f"Sophia events fetch failed: {exc}")


STATUS_SCHEMA = {
    "name": "sophia_voice_status",
    "description": "Check the Sophia voice sidecar status, active sessions, model profile, and protocol.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

INTENT_SCHEMA = {
    "name": "sophia_voice_intent",
    "description": "Classify a voice transcript and return the Hermes prompt Sophia would use.",
    "parameters": {
        "type": "object",
        "properties": {"transcript": {"type": "string", "description": "Voice transcript text."}},
        "required": ["transcript"],
        "additionalProperties": False,
    },
}

CHAT_SCHEMA = {
    "name": "sophia_voice_chat",
    "description": "Send a voice transcript through Sophia's Hermes-aware voice chat path.",
    "parameters": {
        "type": "object",
        "properties": {
            "transcript": {"type": "string", "description": "Voice transcript text."},
            "session_id": {"type": "string", "description": "Optional Sophia session id."},
            "user_id": {"type": "string", "description": "Optional speaker/user id."},
        },
        "required": ["transcript"],
        "additionalProperties": False,
    },
}

EVENTS_SCHEMA = {
    "name": "sophia_voice_events",
    "description": "Fetch recent Sophia voice sidecar events for dashboard or debugging views.",
    "parameters": {
        "type": "object",
        "properties": {
            "after_id": {"type": "integer", "description": "Only return events after this event id."},
            "session_id": {"type": "string", "description": "Optional session id filter."},
        },
        "additionalProperties": False,
    },
}


def register(ctx) -> None:
    ctx.register_tool(
        name="sophia_voice_status",
        toolset="sophia_voice",
        schema=STATUS_SCHEMA,
        handler=_handle_status,
        emoji="🎙️",
    )
    ctx.register_tool(
        name="sophia_voice_intent",
        toolset="sophia_voice",
        schema=INTENT_SCHEMA,
        handler=_handle_intent,
        emoji="🎙️",
    )
    ctx.register_tool(
        name="sophia_voice_chat",
        toolset="sophia_voice",
        schema=CHAT_SCHEMA,
        handler=_handle_chat,
        emoji="🎙️",
    )
    ctx.register_tool(
        name="sophia_voice_events",
        toolset="sophia_voice",
        schema=EVENTS_SCHEMA,
        handler=_handle_events,
        emoji="🎙️",
    )
