"""Single configuration-loading facade for hermes-agent (LLD W-84).

Historically three divergent config paths existed:

* ``cli.py:load_cli_config()``  — CLI-mode loader with its own ``defaults`` dict.
* ``hermes_cli/config.py:load_config()`` — canonical loader built on
  ``DEFAULT_CONFIG`` (the single source of truth for config keys).
* ``gateway/run.py`` + ``gateway/config.py`` — read the user YAML *raw*,
  bypassing DEFAULT_CONFIG entirely.

This module is the consolidation point. The canonical defaults live in
``hermes_cli/config.DEFAULT_CONFIG``; every loader should ultimately resolve
through here so a key added in one place is visible everywhere.

Scope of this change: introduce the facade + an example-vs-defaults
validation helper, and a CI-runner script. Full routing of ``cli.py`` and
the gateway onto this facade is tracked as a phased TODO (see
docs/remediation-todos.md) because both have load-bearing divergence (CLI
personalities, gateway env bridging) that must be migrated carefully.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

# Canonical loader — the single source of truth for DEFAULT_CONFIG.
from hermes_cli.config import DEFAULT_CONFIG, load_config as _load_config_canonical


def load_config() -> Dict[str, Any]:
    """Public facade: load the merged config via the canonical loader.

    New code should call ``config.loader.load_config()`` instead of reaching
    into ``hermes_cli.config`` or re-implementing defaults. This keeps one
    merge path and one DEFAULT_CONFIG as the contract.
    """
    return _load_config_canonical()


def load_config_readonly() -> Dict[str, Any]:
    """Read-only fast-path facade (see hermes_cli.config.load_config_readonly)."""
    from hermes_cli.config import load_config_readonly as _ro

    return _ro()


def get_default_config() -> Dict[str, Any]:
    """Return a deep copy of the canonical DEFAULT_CONFIG tree."""
    return copy.deepcopy(DEFAULT_CONFIG)


def effective_defaults() -> Dict[str, Any]:
    """Return the effective default config tree the CLI actually uses.

    This is ``DEFAULT_CONFIG`` deep-merged with the CLI loader's own defaults
    (e.g. the ``personalities`` map). Used by the example-config validator so
    it checks against what the CLI produces, not just the bare DEFAULT_CONFIG.
    """
    merged = get_default_config()
    try:
        import cli  # noqa: F401  (heavy, but only used by the CI validator)

        cli_defaults = cli.load_cli_config()
        _deep_update(merged, cli_defaults)
    except Exception:
        # If cli can't be imported in the validation context, fall back to
        # DEFAULT_CONFIG alone.
        pass
    return merged


def _deep_update(base: dict, overrides: dict) -> dict:
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def validate_example_against_defaults(example_path: str) -> list[str]:
    """Assert every key in a ``cli-config.yaml.example`` exists in the effective defaults.

    Returns a list of missing dot-paths (empty == valid). Only structural
    presence is checked (keys + nested dicts); scalar values are not compared.
    """
    import yaml

    with open(example_path, "r", encoding="utf-8") as fh:
        example = yaml.safe_load(fh) or {}

    defaults = effective_defaults()
    missing: list[str] = []

    def walk(ex: Any, dflt: Any, path: str) -> None:
        if not isinstance(ex, dict):
            return
        if not isinstance(dflt, dict):
            missing.append(path)
            return
        for key, val in ex.items():
            child = f"{path}.{key}" if path else key
            if key not in dflt:
                missing.append(child)
            else:
                walk(val, dflt[key], child)

    walk(example, defaults, "")
    return missing
