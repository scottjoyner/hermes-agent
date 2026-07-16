"""Light-mode color remapping helpers (extracted from cli.py — LLD W-78).

These functions remap dark-mode-tuned skin colors to higher-contrast
equivalents when the terminal is detected as light mode. They were pulled out
of the ~15k-LOC ``cli.py`` to continue its phased decomposition into
``hermes_cli/`` submodules. ``cli.py`` re-imports them so call sites and
behavior are unchanged.

``_detect_light_mode`` and ``_LIGHT_MODE_REMAP`` remain defined in ``cli.py``
(the former is broadly used there and the latter is the raw remap table); this
module imports them lazily to avoid a circular import.
"""

from __future__ import annotations


def _maybe_remap_for_light_mode(hex_color: str) -> str:
    """If we're in light mode, remap a dark-mode-tuned color to a
    higher-contrast equivalent.  No-op in dark mode."""
    from cli import _detect_light_mode, _LIGHT_MODE_REMAP

    if not _detect_light_mode():
        return hex_color
    if not hex_color or not hex_color.startswith("#"):
        return hex_color
    # Case-insensitive lookup (build the uppercased table lazily)
    upper = hex_color.upper()
    remap_upper = {k.upper(): v for k, v in _LIGHT_MODE_REMAP.items()}
    if upper in remap_upper:
        return remap_upper[upper]
    return hex_color


def _install_skin_light_mode_hook() -> None:
    """Wrap SkinConfig.get_color at import time so EVERY skin color read goes
    through the light-mode remap.  Idempotent."""
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
