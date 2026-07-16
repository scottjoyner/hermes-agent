"""ANSI color helpers plus light-mode remapping hooks.

This module intentionally stays tiny and import-safe because many Hermes CLI
modules import it during startup.  It provides the legacy ``Colors`` namespace
and ``color()`` helper expected throughout the codebase, while also installing
an optional skin light-mode remap hook.
"""

from __future__ import annotations


class Colors:
    """Legacy ANSI color namespace used by the Hermes CLI."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"


def color(text: str, code: str = "") -> str:
    """Wrap text in ANSI color codes when a code is supplied."""
    if not code:
        return text
    return f"{code}{text}{Colors.RESET}"


def _maybe_remap_for_light_mode(hex_color: str) -> str:
    """If we're in light mode, remap a dark-mode-tuned color to a
    higher-contrast equivalent.  No-op in dark mode."""
    from cli import _detect_light_mode, _LIGHT_MODE_REMAP

    if not _detect_light_mode():
        return hex_color
    if not hex_color or not hex_color.startswith("#"):
        return hex_color
    upper = hex_color.upper()
    remap_upper = {k.upper(): v for k, v in _LIGHT_MODE_REMAP.items()}
    if upper in remap_upper:
        return remap_upper[upper]
    return hex_color


def _install_skin_light_mode_hook() -> None:
    """Wrap SkinConfig.get_color at import time so every skin color read goes
    through the light-mode remap. Idempotent."""
    try:
        from hermes_cli.skin_engine import SkinConfig  # type: ignore[import]
    except Exception:
        return
    if getattr(SkinConfig, "_hermes_light_mode_hook_installed", False):
        return
    _orig_get_color = SkinConfig.get_color

    def _wrapped_get_color(self, key, fallback=""):
        value = _orig_get_color(self, key, fallback)
        try:
            return _maybe_remap_for_light_mode(value)
        except Exception:
            return value

    SkinConfig.get_color = _wrapped_get_color  # type: ignore[method-assign]
    SkinConfig._hermes_light_mode_hook_installed = True  # type: ignore[attr-defined]


_install_skin_light_mode_hook()
