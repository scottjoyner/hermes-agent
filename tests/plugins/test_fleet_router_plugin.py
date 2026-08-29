from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "fleet-router"


def _load_module():
    name = "test_fleet_router_plugin_impl"
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


def _config(nodes, **fleet_overrides):
    return {
        "fleet": {
            "enabled": True,
            "nodes": nodes,
            **fleet_overrides,
        }
    }


def _healthy(router, latency_by_name=None):
    latency_by_name = latency_by_name or {}
    now = time.time()
    for name, state in router._nodes.items():
        state.healthy = True
        state.last_probe = now
        state.models = state.config.models
        latency = float(latency_by_name.get(name, 10.0))
        state.latency_ms = latency
        state.latency_ema_ms = latency


def test_config_uses_only_explicit_nodes_and_rejects_public_urls_without_opt_in():
    module = _load_module()
    parsed = module.core.parse_fleet_config(
        _config(
            [
                {
                    "name": "private",
                    "base_url": "http://x1-370.lan:1234/v1",
                },
                {
                    "name": "public-denied",
                    "base_url": "https://example.com/v1",
                },
                {
                    "name": "public-explicit",
                    "base_url": "https://example.net/v1",
                    "allow_remote": True,
                },
                {
                    "name": "invalid",
                    "base_url": "file:///tmp/socket",
                },
            ]
        )
    )

    assert [node.name for node in parsed.nodes] == [
        "private",
        "public-explicit",
    ]
    assert all("kipnerter" not in node.base_url for node in parsed.nodes)


def test_credentials_are_node_specific_and_inbound_authorization_is_not_forwarded(
    monkeypatch,
):
    module = _load_module()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("X1_FLEET_KEY", "node-secret")
    parsed = module.core.parse_fleet_config(
        _config(
            [
                {
                    "name": "no-key",
                    "base_url": "http://node-a.lan:1234/v1",
                    "models": ["model-a"],
                },
                {
                    "name": "explicit-key",
                    "base_url": "http://node-b.lan:1234/v1",
                    "models": ["model-a"],
                    "api_key_env": "X1_FLEET_KEY",
                },
            ]
        )
    )
    router = module.FleetRouter(parsed)

    no_key = router.upstream_headers(
        parsed.nodes[0],
        {
            "Authorization": "Bearer inbound",
            "X-Hermes-Cache-Checkpoint-Id": "abc",
        },
    )
    explicit = router.upstream_headers(
        parsed.nodes[1],
        {"Authorization": "Bearer inbound"},
    )

    assert "Authorization" not in no_key
    assert no_key["X-Hermes-Cache-Checkpoint-Id"] == "abc"
    assert explicit["Authorization"] == "Bearer node-secret"


def test_router_rejects_insufficient_context_and_selects_headroom():
    module = _load_module()
    parsed = module.core.parse_fleet_config(
        _config(
            [
                {
                    "name": "small",
                    "base_url": "http://small.lan:1234/v1",
                    "models": ["qwen"],
                    "context_length": 8192,
                },
                {
                    "name": "large",
                    "base_url": "http://large.lan:1234/v1",
                    "models": ["qwen"],
                    "context_length": 32768,
                },
            ]
        )
    )
    router = module.FleetRouter(parsed)
    _healthy(router)

    decision = router.route(
        module.RouteRequirements(
            "qwen",
            7500,
            2000,
        )
    )

    assert decision is not None
    assert decision.node_name == "large"
    assert decision.required_context > 8192
    assert decision.context_headroom > 0


def test_capability_filters_and_model_alias_mapping():
    module = _load_module()
    parsed = module.core.parse_fleet_config(
        _config(
            [
                {
                    "name": "text",
                    "base_url": "http://text.lan:1234/v1",
                    "models": ["real-model"],
                    "model_map": {"friendly": "real-model"},
                    "supports_vision": False,
                },
                {
                    "name": "vision",
                    "base_url": "http://vision.lan:1234/v1",
                    "models": ["real-model"],
                    "model_map": {"friendly": "real-model"},
                    "supports_vision": True,
                },
            ]
        )
    )
    router = module.FleetRouter(parsed)
    _healthy(router)

    decision = router.route(
        module.RouteRequirements(
            "friendly",
            100,
            100,
            needs_vision=True,
        )
    )

    assert decision is not None
    assert decision.node_name == "vision"
    assert decision.upstream_model == "real-model"


def test_checkpoint_affinity_beats_small_latency_difference():
    module = _load_module()
    parsed = module.core.parse_fleet_config(
        _config(
            [
                {
                    "name": "fast",
                    "base_url": "http://fast.lan:1234/v1",
                    "models": ["model"],
                    "context_length": 32768,
                },
                {
                    "name": "cached",
                    "base_url": "http://cached.lan:1234/v1",
                    "models": ["model"],
                    "context_length": 32768,
                },
            ]
        )
    )
    router = module.FleetRouter(parsed)
    _healthy(router, {"fast": 5.0, "cached": 20.0})
    cached = next(
        decision
        for decision in router.rank(
            module.RouteRequirements("model", 100, 100)
        )
        if decision.node_name == "cached"
    )
    router.release(
        cached,
        True,
        20.0,
        checkpoint_id="checkpoint-1",
    )

    chosen = router.route(
        module.RouteRequirements(
            "model",
            100,
            100,
            checkpoint_id="checkpoint-1",
        )
    )

    assert chosen is not None
    assert chosen.node_name == "cached"
    assert "checkpoint-affinity" in chosen.reasons


def test_inflight_load_penalty_uses_other_healthy_node():
    module = _load_module()
    parsed = module.core.parse_fleet_config(
        _config(
            [
                {
                    "name": "busy",
                    "base_url": "http://busy.lan:1234/v1",
                    "models": ["model"],
                    "max_concurrency": 1,
                },
                {
                    "name": "idle",
                    "base_url": "http://idle.lan:1234/v1",
                    "models": ["model"],
                    "max_concurrency": 1,
                },
            ]
        )
    )
    router = module.FleetRouter(parsed)
    _healthy(router)
    first = router.route(module.RouteRequirements("model", 10, 10))
    assert first is not None and first.node_name == "busy"
    router.acquire(first)

    second = router.route(module.RouteRequirements("model", 10, 10))

    assert second is not None
    assert second.node_name == "idle"


def test_unknown_models_are_fail_closed_unless_explicitly_allowed():
    module = _load_module()
    parsed = module.core.parse_fleet_config(
        _config(
            [
                {
                    "name": "strict",
                    "base_url": "http://strict.lan:1234/v1",
                    "models": ["known"],
                },
                {
                    "name": "wildcard",
                    "base_url": "http://wildcard.lan:1234/v1",
                    "accept_unknown_models": True,
                },
            ]
        )
    )
    router = module.FleetRouter(parsed)
    _healthy(router)

    decision = router.route(
        module.RouteRequirements(
            "new-model",
            10,
            10,
        )
    )

    assert decision is not None
    assert decision.node_name == "wildcard"
    assert decision.upstream_model == "new-model"


def test_requirements_detect_tools_vision_reasoning_and_cache_affinity():
    module = _load_module()
    requirements = module.core.requirements_from_payload(
        {
            "model": "vision-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,AA"
                            },
                        },
                    ],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read"},
                }
            ],
            "reasoning": {"effort": "low"},
            "max_completion_tokens": 777,
        },
        {
            "X-Hermes-Cache-Session-Id": "session-1",
            "X-Hermes-Cache-Checkpoint-Id": "checkpoint-1",
        },
    )

    assert requirements.needs_tools is True
    assert requirements.needs_vision is True
    assert requirements.needs_reasoning is True
    assert requirements.max_output_tokens == 777
    assert requirements.session_id == "session-1"
    assert requirements.checkpoint_id == "checkpoint-1"
    assert requirements.input_tokens > 1


def test_join_endpoint_avoids_double_v1():
    module = _load_module()
    assert module.core.join_endpoint(
        "http://node.lan:1234/v1",
        "/v1/chat/completions",
    ) == "http://node.lan:1234/v1/chat/completions"
    assert module.core.join_endpoint(
        "http://node.lan:1234",
        "/v1/chat/completions",
    ) == "http://node.lan:1234/v1/chat/completions"


def test_non_loopback_bind_is_fail_closed():
    module = _load_module()
    parsed = module.core.parse_fleet_config(
        _config(
            [
                {
                    "name": "node",
                    "base_url": "http://node.lan:1234/v1",
                }
            ],
            listen={"host": "0.0.0.0", "port": 8765},
        )
    )

    with pytest.raises(ValueError, match="non-loopback"):
        module.serve(module.FleetRouter(parsed))


def test_cli_and_plugin_registration():
    module = _load_module()
    registrations = []

    class Context:
        def register_cli_command(self, *args, **kwargs):
            registrations.append((args, kwargs))

    module.register(Context())
    assert registrations[0][0][0] == "fleet"

    parser = argparse.ArgumentParser()
    module._setup_cli(parser)
    args = parser.parse_args(
        [
            "route",
            "--model",
            "test-model",
            "--input-tokens",
            "10",
            "--vision",
        ]
    )
    assert args.fleet_command == "route"
    assert args.model == "test-model"
    assert args.input_tokens == 10
    assert args.vision is True
