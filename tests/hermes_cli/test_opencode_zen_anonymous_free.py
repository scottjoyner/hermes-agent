from __future__ import annotations

import json
import os
import subprocess

import pytest

from hermes_cli.auth import AuthError
from hermes_cli.runtime_provider import resolve_runtime_provider


def test_free_opencode_zen_reuses_anonymous_cli_contract(monkeypatch):
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "unrelated-provider-secret")

    resolved = resolve_runtime_provider(
        requested="opencode-zen", target_model="space-bunny-free"
    )

    assert resolved["provider"] == "opencode-zen"
    assert resolved["base_url"] == "https://opencode.ai/zen/v1"
    assert resolved["api_key"] == ""
    assert resolved["source"] == "opencode-cli-anonymous-free"


def test_nonfree_opencode_zen_still_fails_closed_without_zen_auth(monkeypatch):
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(AuthError):
        resolve_runtime_provider(
            requested="opencode-zen", target_model="claude-sonnet-4-6"
        )


def test_unrelated_provider_key_is_never_accepted_as_zen_auth(monkeypatch):
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "unrelated-provider-secret")

    with pytest.raises(AuthError):
        resolve_runtime_provider(
            requested="opencode-zen", target_model="claude-sonnet-4-6"
        )


def test_doctor_evidence_contains_no_secret_material():
    env = os.environ.copy()
    env.pop("OPENCODE_ZEN_API_KEY", None)
    result = subprocess.run(
        ["/home/scott/bin/hermes-session-doctor", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    payload = json.loads(result.stdout)
    rendered = result.stdout + result.stderr
    assert "api_key" not in rendered
    assert "OPENCODE_ZEN_API_KEY" not in rendered
    assert payload["provider_resolution"]["ok"] is True
