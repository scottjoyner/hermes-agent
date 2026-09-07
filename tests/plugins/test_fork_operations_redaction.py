from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "fork-operations"
    / "__init__.py"
)


def _load_plugin():
    name = "hermes_test_fork_operations_redaction"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_redacts_url_credentials_query_and_fragment():
    plugin = _load_plugin()

    assert plugin._redact_url(
        "https://token:secret@example.com/org/repo.git?key=value#fragment"
    ) == "https://example.com/org/repo.git"
    assert plugin._redact_url(
        "neo4j://neo4j:password@graph.internal:7687/neo4j?routing=true"
    ) == "neo4j://graph.internal:7687/neo4j"
    assert plugin._redact_url(
        "git@github.com:scottjoyner/hermes-agent.git"
    ) == "github.com:scottjoyner/hermes-agent.git"


def test_check_details_never_serialize_embedded_credentials():
    plugin = _load_plugin()
    checks = []

    plugin._add(
        checks,
        "remote",
        "ok",
        "configured",
        url="https://token:secret@example.com/repo?x=1",
        uri="bolt://neo4j:password@graph.internal:7687",
    )
    serialized = json.dumps(checks[0].to_dict())

    assert "token" not in serialized
    assert "secret" not in serialized
    assert "password" not in serialized
    assert "?x=1" not in serialized
    assert "example.com/repo" in serialized
    assert "graph.internal:7687" in serialized


def test_truthy_handles_quoted_false_values():
    plugin = _load_plugin()

    assert plugin._truthy(False) is False
    assert plugin._truthy("false") is False
    assert plugin._truthy("0") is False
    assert plugin._truthy("yes") is True
