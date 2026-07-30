"""Conservative tokenizer-independent context sizing for fleet requests."""

from __future__ import annotations

from typing import Any, Mapping

from .core import (
    RouteRequirements,
    estimate_tokens,
    integer,
    request_has_vision,
)

_CONTROL_FIELDS = {
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "max_completion_tokens",
    "max_tokens",
    "n",
    "parallel_tool_calls",
    "presence_penalty",
    "seed",
    "service_tier",
    "stop",
    "stream",
    "stream_options",
    "temperature",
    "timeout",
    "top_logprobs",
    "top_p",
    "user",
}
_IMAGE_TYPES = {"image", "image_url", "input_image"}


def _tokenizable(value: Any) -> Any:
    """Remove transport bulk while preserving prompt/schema structure.

    Base64 image bytes are not text tokens and can be many megabytes. Replace
    image payloads with a stable marker so vision requests reserve some context
    without being rejected according to encoded byte size.
    """

    if isinstance(value, list):
        return [_tokenizable(item) for item in value]
    if not isinstance(value, Mapping):
        return value

    kind = str(value.get("type") or "").strip().lower()
    if kind in _IMAGE_TYPES:
        return {"type": kind, "image": "<image>"}

    shaped: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if normalized == "image_url":
            shaped[str(key)] = "<image>"
        elif normalized in {"data", "b64_json"} and isinstance(item, str):
            shaped[str(key)] = "<binary>"
        else:
            shaped[str(key)] = _tokenizable(item)
    return shaped


def requirements_from_payload(
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    default_max_output: int = 4096,
) -> RouteRequirements:
    """Build route requirements from all context-bearing request fields."""

    lowered = {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
    }
    output = integer(
        payload.get("max_completion_tokens") or payload.get("max_tokens"),
        default_max_output,
        1,
    )
    context_envelope = {
        str(key): _tokenizable(value)
        for key, value in payload.items()
        if str(key).lower() not in _CONTROL_FIELDS
    }
    reasoning = payload.get("reasoning") or payload.get("reasoning_effort")
    needs_reasoning = (
        reasoning.get("enabled") is not False
        if isinstance(reasoning, Mapping)
        else bool(reasoning)
    )
    return RouteRequirements(
        model=str(payload.get("model") or "").strip(),
        input_tokens=estimate_tokens(context_envelope),
        max_output_tokens=output,
        needs_tools=bool(payload.get("tools")),
        needs_vision=request_has_vision(payload),
        needs_reasoning=needs_reasoning,
        session_id=(
            lowered.get("x-hermes-cache-session-id")
            or lowered.get("x-hermes-session-id", "")
        ),
        checkpoint_id=lowered.get(
            "x-hermes-cache-checkpoint-id",
            "",
        ),
    )


__all__ = ["requirements_from_payload"]
