"""Gateway status-message helpers (extracted from gateway/run.py — LLD W-77).

These are the standalone, platform-agnostic helpers that prepare and route
agent status callbacks to messaging platforms. They were extracted out of the
~18.5k-LOC ``gateway/run.py`` to begin its phased decomposition. ``run.py``
re-imports them so behavior is unchanged.

The helper functions depend on a handful of module-level names in
``gateway.run`` (redaction regexes, platform-value helpers). To avoid an
import cycle (run.py imports this module at module load), those names are
imported lazily *inside* each function body — by the time the functions are
called, ``gateway.run`` is fully initialized.
"""

from __future__ import annotations

from typing import Any, Optional


def _prepare_gateway_status_message(platform: Any, event_type: str, message: str) -> Optional[str]:
    """Filter/sanitize agent status callbacks before platform delivery."""
    from gateway import run as _run

    text = str(message or "").strip()
    if not text:
        return None
    if _run._gateway_platform_value(platform) != "telegram":
        return text

    text = _run._redact_gateway_user_facing_secrets(text)
    if _run._TELEGRAM_NOISY_STATUS_RE.search(text):
        return None
    if _run._looks_like_gateway_provider_error(text):
        return _run._gateway_provider_error_reply(text)
    return text


async def _send_or_update_status_coro(adapter, chat_id, status_key, content, metadata):
    """Route a status message through adapter.send_or_update_status when supported.

    Issue #30045: adapters that implement send_or_update_status (currently
    Telegram) edit the previous bubble for the same status_key instead of
    appending a new one. Adapters without the method fall back to plain send.
    """
    sender = getattr(adapter, "send_or_update_status", None)
    if callable(sender):
        return await sender(chat_id, status_key, content, metadata=metadata)
    return await adapter.send(chat_id, content, metadata=metadata)
