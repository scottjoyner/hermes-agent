"""Paperclip integration gate (LLD W-80).

Hermes-agent can be driven by Paperclip (an agent-to-agent orchestration
platform) via the separately-cloned ``hermes-paperclip-adapter`` repo. As of
this writing the adapter is cloned but NOT yet registered as a Paperclip
plugin ("Hermes Agent not yet hired"), so this module only *gates + logs* the
integration — it does not perform registration itself (that lives in the
adapter repo, a separate codebase).

The flag resolution order is:

1. Explicit ``gateway.paperclip.integration_enabled`` in ``config.yaml``
   (``True`` / ``False``).  ``None`` falls through to auto-detection.
2. ``PAPERCLIP_INTEGRATION_ENABLED`` env var (``1``/``true`` => enabled).
3. Auto-detect: enabled when the adapter repo directory exists at
   ``HERMES_PAPERCLIP_ADAPTER_DIR`` (default ``./hermes-paperclip-adapter``)
   relative to the repo root, otherwise disabled.

Call :func:`report_paperclip_integration_status` once at gateway startup to
emit the appropriate log line (registration expected, or transitional
"not registered" warning).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

logger = None  # bound lazily to avoid import cycle with gateway.run


def _default_adapter_dir() -> Path:
    # Repo root is two levels up from this file (agent/ -> hermes-agent/).
    return Path(__file__).resolve().parent.parent / "hermes-paperclip-adapter"


def resolve_paperclip_integration_enabled(config: Optional[dict] = None) -> bool:
    """Resolve whether Paperclip integration is enabled.

    ``config`` is the gateway/merged config dict; only the
    ``gateway.paperclip.integration_enabled`` key is consulted. When that key
    is ``None`` (the default), auto-detection is used.
    """
    explicit = None
    if isinstance(config, dict):
        paperclip_cfg = config.get("gateway", {}).get("paperclip", {})
        if isinstance(paperclip_cfg, dict):
            explicit = paperclip_cfg.get("integration_enabled", None)

    if explicit is True:
        return True
    if explicit is False:
        return False

    env = os.getenv("PAPERCLIP_INTEGRATION_ENABLED")
    if env is not None and env.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if env is not None and env.strip().lower() in ("0", "false", "no", "off"):
        return False

    adapter_dir = os.getenv("HERMES_PAPERCLIP_ADAPTER_DIR")
    candidate = Path(adapter_dir) if adapter_dir else _default_adapter_dir()
    return candidate.exists()


def report_paperclip_integration_status(config: Optional[dict] = None) -> bool:
    """Log the Paperclip integration status at gateway startup.

    Returns ``True`` when integration is enabled (registration expected),
    ``False`` when transitional / not registered. Safe to call once per
    gateway start; does not raise.
    """
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger("gateway.paperclip")

    enabled = resolve_paperclip_integration_enabled(config)
    if enabled:
        logger.info(
            "Paperclip integration ENABLED: hermes-paperclip-adapter is expected "
            "to be registered with the Paperclip server. If registration is "
            "missing, install it via ~/.paperclip/adapter-plugins.json "
            "(see HANDOFF-PAPERCLIP-INTEGRATION.md)."
        )
    else:
        logger.warning(
            "transitional: Paperclip integration NOT registered — Hermes Agent "
            "is running standalone. The hermes-paperclip-adapter repo is cloned "
            "but not yet registered as a Paperclip plugin ('Hermes Agent not yet "
            "hired'). Set gateway.paperclip.integration_enabled: true (or "
            "PAPERCLIP_INTEGRATION_ENABLED=1) once registration is complete; "
            "see HANDOFF-PAPERCLIP-INTEGRATION.md."
        )
    return enabled
