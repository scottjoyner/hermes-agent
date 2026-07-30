"""Load the small-model context middleware before cache-foundation.

Bundled plugins register middleware in sorted directory-discovery order. This
small loader intentionally sorts before ``cache-foundation`` so cache manifests
and affinity headers fingerprint the already-optimized provider request.

The implementation and operator documentation remain in the descriptive
``plugins/small-model-context/`` directory, which has no manifest and therefore
is not independently discovered as a second plugin.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_IMPLEMENTATION_MODULE = "hermes_plugins.small_model_context_impl"
_IMPLEMENTATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "small-model-context"
    / "__init__.py"
)


def _implementation() -> ModuleType:
    cached = sys.modules.get(_IMPLEMENTATION_MODULE)
    if cached is not None:
        return cached

    spec = importlib.util.spec_from_file_location(
        _IMPLEMENTATION_MODULE,
        _IMPLEMENTATION_PATH,
    )
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Could not load small-model context implementation: "
            f"{_IMPLEMENTATION_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_IMPLEMENTATION_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_IMPLEMENTATION_MODULE, None)
        raise
    return module


def register(ctx: Any) -> None:
    """Delegate plugin registration to the implementation module."""

    _implementation().register(ctx)


__all__ = ["register"]
