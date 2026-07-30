"""Hermes local-model fleet and Headroom router plugin."""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import asdict
from typing import Any, Mapping

from .core import (
    FleetConfig,
    FleetNodeConfig,
    FleetRouter,
    RouteDecision,
    RouteRequirements,
    parse_fleet_config,
)
from .proxy import is_loopback_bind, serve

_ROUTER: FleetRouter | None = None
_ROUTER_LOCK = threading.RLock()


def _load_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        value = load_config_readonly() or {}
    except Exception:
        try:
            from hermes_cli.config import load_config

            value = load_config() or {}
        except Exception:
            value = {}
    return value if isinstance(value, dict) else {}


def get_router(
    config: Mapping[str, Any] | None = None,
    *,
    reset: bool = False,
) -> FleetRouter:
    global _ROUTER
    with _ROUTER_LOCK:
        if reset or _ROUTER is None:
            root = dict(config) if isinstance(config, Mapping) else _load_config()
            _ROUTER = FleetRouter(parse_fleet_config(root))
        return _ROUTER


def _print_payload(payload: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
        if key not in {"nodes", "routes"}:
            print(f"{key:24}: {value}")
    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, Mapping):
                print(
                    f"- {node.get('name')}: healthy={node.get('healthy')} "
                    f"inflight={node.get('inflight')}/"
                    f"{node.get('max_concurrency')} "
                    f"latency={node.get('latency_ema_ms')}ms "
                    f"models={len(node.get('models') or [])}"
                )
    routes = payload.get("routes")
    if isinstance(routes, list):
        for route in routes:
            if isinstance(route, Mapping):
                print(
                    f"- {route.get('node_name')}: "
                    f"score={route.get('score')} "
                    f"model={route.get('upstream_model')} "
                    f"headroom={route.get('context_headroom')}"
                )


def _doctor(router: FleetRouter) -> dict[str, Any]:
    status = router.refresh(force=True)
    errors: list[str] = []
    if not router.config.enabled:
        errors.append("fleet.enabled is false")
    if not router.config.nodes:
        errors.append("no valid explicit nodes configured")
    if (
        not is_loopback_bind(router.config.listen_host)
        and not router.config.allow_non_loopback
    ):
        errors.append("non-loopback listen host is not explicitly allowed")
    for node in status["nodes"]:
        if not node["healthy"]:
            errors.append(
                f"{node['name']}: {node['last_error'] or 'unhealthy'}"
            )
    return {**status, "ok": not errors, "errors": errors}


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="fleet_command")

    status = commands.add_parser("status", help="Show configured fleet state")
    status.add_argument("--json", action="store_true")
    status.add_argument("--refresh", action="store_true")

    discover = commands.add_parser(
        "discover",
        help="Concurrently probe explicitly configured fleet nodes",
    )
    discover.add_argument("--json", action="store_true")

    doctor = commands.add_parser(
        "doctor",
        help="Validate config and probe every node",
    )
    doctor.add_argument("--json", action="store_true")

    route = commands.add_parser(
        "route",
        help="Explain which node would receive a request",
    )
    route.add_argument("--model", required=True)
    route.add_argument("--input-tokens", type=int, default=1)
    route.add_argument("--max-output-tokens", type=int, default=4096)
    route.add_argument("--tools", action="store_true")
    route.add_argument("--vision", action="store_true")
    route.add_argument("--reasoning", action="store_true")
    route.add_argument("--session", default="")
    route.add_argument("--checkpoint", default="")
    route.add_argument("--json", action="store_true")

    serve_command = commands.add_parser(
        "serve",
        help="Run the local OpenAI-compatible proxy",
    )
    serve_command.add_argument("--host", default="")
    serve_command.add_argument("--port", type=int, default=0)
    parser.set_defaults(func=_handle_cli)


def _handle_cli(args: argparse.Namespace) -> int:
    router = get_router(reset=True)
    command = getattr(args, "fleet_command", None)
    as_json = bool(getattr(args, "json", False))

    if command == "status":
        if bool(getattr(args, "refresh", False)):
            router.refresh(force=True)
        _print_payload(router.status(), as_json)
        return 0
    if command == "discover":
        payload = router.refresh(force=True)
        _print_payload(payload, as_json)
        return 0 if payload["healthy_nodes"] else 1
    if command == "doctor":
        payload = _doctor(router)
        _print_payload(payload, as_json)
        return 0 if payload["ok"] else 1
    if command == "route":
        router.refresh()
        requirements = RouteRequirements(
            model=str(getattr(args, "model", "")),
            input_tokens=max(1, int(getattr(args, "input_tokens", 1))),
            max_output_tokens=max(
                1,
                int(getattr(args, "max_output_tokens", 4096)),
            ),
            needs_tools=bool(getattr(args, "tools", False)),
            needs_vision=bool(getattr(args, "vision", False)),
            needs_reasoning=bool(getattr(args, "reasoning", False)),
            session_id=str(getattr(args, "session", "")),
            checkpoint_id=str(getattr(args, "checkpoint", "")),
        )
        payload = {
            "requirements": asdict(requirements),
            "routes": [
                decision.to_dict()
                for decision in router.rank(requirements)
            ],
        }
        _print_payload(payload, as_json)
        return 0 if payload["routes"] else 1
    if command == "serve":
        serve(
            router,
            host=str(getattr(args, "host", "") or "") or None,
            port=int(getattr(args, "port", 0) or 0) or None,
        )
        return 0

    print("usage: hermes fleet {status,discover,doctor,route,serve}")
    return 2


def register(ctx: Any) -> None:
    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(
            "fleet",
            "Discover, inspect, route, and serve a local inference fleet",
            _setup_cli,
            _handle_cli,
            description=(
                "Headroom-aware OpenAI-compatible local model fleet proxy"
            ),
        )


__all__ = [
    "FleetConfig",
    "FleetNodeConfig",
    "FleetRouter",
    "RouteDecision",
    "RouteRequirements",
    "get_router",
    "register",
    "serve",
]
