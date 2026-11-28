"""Regression tests for the gateway startup fallback-chain warning.

A gateway whose config has no usable fallback chain cannot fail over from a
local-only provider (e.g. LM Studio) on a transient transport failure. The
startup reader now logs an actionable warning in that case, while a configured
chain must stay silent.
"""

from __future__ import annotations

import logging


def _load_with_config(tmp_path, monkeypatch, config_text):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(config_text, encoding="utf-8")
    return GatewayRunner._load_fallback_model()


def test_warns_once_when_config_has_no_fallback_chain(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        chain = _load_with_config(
            tmp_path,
            monkeypatch,
            "model:\n  provider: nvidia\nfallback_providers: []\n",
        )

    assert chain is None
    warnings = [
        r for r in caplog.records
        if "No fallback_providers configured" in r.message
    ]
    assert len(warnings) == 1
    assert "will be retried but not failed over" in warnings[0].message


def test_no_warning_when_fallback_chain_is_present(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        chain = _load_with_config(
            tmp_path,
            monkeypatch,
            "fallback_providers:\n"
            "  - provider: deepseek\n"
            "    model: deepseek-v4-flash\n",
        )

    assert chain == [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    assert not any("No fallback_providers configured" in r.message
                   for r in caplog.records)
