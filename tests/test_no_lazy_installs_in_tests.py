"""Unit tests must never shell out to PyPI.

Importing an opt-in backend (``plugins.memory.mem0`` and friends) calls
``tools.lazy_deps.ensure()``, which runs ``uv pip install`` against the
network. Inside a test that is a live dependency on PyPI reachability: on a
good day it costs ~17s, and when PyPI is slow it hangs until the 300s per-file
SIGKILL and fails a shard for reasons that have nothing to do with the diff
under test.

``tests/conftest.py`` seals the venv (``HERMES_DISABLE_LAZY_INSTALLS=1`` with
no durable target) so ``ensure()`` raises ``FeatureUnavailable`` instead of
installing. These tests hold that seal in place.
"""

from __future__ import annotations

import os

import pytest

import tools.lazy_deps as ld


def test_lazy_installs_are_sealed_for_every_test():
    """The autouse hermetic fixture must leave installs disabled."""
    assert os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1"
    # A durable target would UN-seal the venv (installs get redirected there
    # rather than blocked), so the seal depends on it staying unset.
    assert not os.environ.get("HERMES_LAZY_INSTALL_TARGET")
    assert ld._allow_lazy_installs() is False


def test_ensure_refuses_to_install_a_missing_backend(monkeypatch):
    """A missing lazy backend surfaces as an error, never as a pip subprocess."""
    monkeypatch.setitem(ld.LAZY_DEPS, "test.sealed", ("packageX==1.0",))
    monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)

    def explode(*_a, **_k):  # pragma: no cover — must never run
        raise AssertionError("ensure() shelled out to pip inside a test")

    monkeypatch.setattr(ld, "_venv_pip_install", explode)

    with pytest.raises(ld.FeatureUnavailable, match="lazy installs disabled"):
        ld.ensure("test.sealed", prompt=False)


def test_importing_a_lazy_memory_backend_does_not_install(monkeypatch):
    """The exact path that hung CI: constructing the mem0 provider.

    ``_create_backend`` calls ``ensure("memory.mem0")`` before importing the
    SDK, and swallows whatever it raises — so a raising stub would be hidden.
    Count the calls instead: under the seal the installer must not be reached
    at all.
    """
    from plugins.memory.mem0 import Mem0MemoryProvider

    calls: list[tuple] = []
    monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
    monkeypatch.setattr(
        ld, "_venv_pip_install",
        lambda specs, **kw: calls.append(specs) or ld._InstallResult(True, "", ""),
    )
    monkeypatch.setattr(
        "plugins.memory.mem0._load_config",
        lambda: {"api_key": "test-key", "agent_id": "hermes"},
    )

    Mem0MemoryProvider().initialize(session_id="sealed-sess")

    assert calls == [], f"mem0 provider shelled out to pip inside a test: {calls}"
