from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "fleet-router"


def _load_module():
    name = "test_fleet_router_external_impl"
    for module_name in list(sys.modules):
        if module_name == name or module_name.startswith(name + "."):
            sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def external_config(**overrides):
    external = {
        "base_url": "http://auto-router-reconciliation:8088/v1",
        "admin_url": "http://auto-router-reconciliation:8088",
        "admin_token_env": "AUTO_ROUTER_ADMIN_TOKEN",
        "default_model": "auto/code",
        "strict_offline": True,
        **overrides,
    }
    return {"fleet": {"mode": "external", "external": external}}


def test_external_mode_forbids_competing_node_registry():
    module = _load_module()
    config = external_config()
    config["fleet"]["nodes"] = [
        {"name": "duplicate", "base_url": "http://xwing.lan:1234/v1"}
    ]

    with pytest.raises(ValueError, match="fleet.nodes is forbidden"):
        module.parse_operating_config(config)


def test_external_mode_rejects_public_gateway_when_strict_offline():
    module = _load_module()

    with pytest.raises(ValueError, match="must be private"):
        module.parse_operating_config(
            external_config(base_url="https://api.example.com/v1")
        )


def test_backward_compatible_node_config_is_standalone():
    module = _load_module()
    parsed = module.parse_operating_config(
        {
            "fleet": {
                "enabled": True,
                "nodes": [
                    {"name": "local", "base_url": "http://local.lan:1234/v1"}
                ],
            }
        }
    )

    assert parsed.mode == "standalone"


def test_get_router_refuses_external_mode():
    module = _load_module()

    with pytest.raises(ValueError, match="only in standalone mode"):
        module.get_router(external_config(), reset=True)


def test_external_status_reads_router_without_local_authority(monkeypatch):
    module = _load_module()
    operating = module.parse_operating_config(external_config())
    assert operating.external is not None
    client = module.ExternalFleetClient(operating.external)

    def fake_get(url, *, admin=False):
        if url.endswith("/health"):
            return 200, {"service": "auto-router"}
        if url.endswith("/v1/models"):
            return 200, {"data": [{"id": "auto/code"}]}
        if url.endswith("/admin/admission"):
            assert admin is True
            return 200, {
                "runtimes": [
                    {
                        "runtime_instance_id": "lmstudio-xwing-1234",
                        "parallel_slots": 1,
                        "active": 0,
                        "queued": 0,
                    }
                ],
                "access_paths": [],
            }
        raise AssertionError(url)

    monkeypatch.setattr(client, "_get", fake_get)
    status = client.status()

    assert status["healthy"] is True
    assert status["mode"] == "external"
    assert status["routing_owner"] == "auto-router"
    assert status["inventory_owner"] == "AssistX/Neo4j"
    assert status["hermes_fleet_proxy_enabled"] is False
    assert status["physical_endpoint_discovery"] is False
    assert status["local_concurrency_authority"] is False
    assert status["models"][0]["id"] == "auto/code"


def test_external_route_preserves_semantic_alias_and_metadata():
    module = _load_module()
    operating = module.parse_operating_config(external_config())
    assert operating.external is not None
    client = module.ExternalFleetClient(operating.external)

    decision = client.route_intent(
        "auto/review",
        {
            "task_id": "task-1",
            "agent_run_id": "run-1",
            "required_capabilities": ["code", "tool_use"],
            "local_only": True,
            "workflow_stage": "review",
            "session_id": "session-1",
            "checkpoint_id": "checkpoint-1",
        },
    )

    assert decision["model_intent"] == "auto/review"
    assert decision["routing_owner"] == "auto-router"
    assert decision["physical_runtime_selected_by_hermes"] is False
    assert decision["metadata"]["task_id"] == "task-1"
