"""Fleet provider plugin — registers the fleet as a provider type.

When provider="fleet" is set in config, Hermes will:
1. Discover available LMStudio endpoints on the tailnet
2. Catalog their models
3. Route each request to the best node for the requested model
4. Forward the response back to the caller transparently

Configuration (in config.yaml or cli-config.yaml):

fleet:
  enabled: true
  tailnet: "kipnerter.ts.net"
  max_latency_ms: 5000
  health_ttl: 300
  discovery_hosts:
    - "destroyer.kipnerter.ts.net"
    - "x1-370.kipnerter.ts.net"
    - "demo-1.kipnerter.ts.net"
  known_hosts:
    - "destroyer"
    - "x1-370"
    - "demo-1"
    - "scotts-macbook-air"
  # Per-model overrides: force specific models to specific nodes
  model_routing:
    "qwen3.6-35b": "destroyer"
    "gemma-4-31b": "x1-370"
    "qwen3.6-27b": "destroyer"
    "qwen3.5-4b": "scott-optiplex-9030-aio"
    "gemma-3-1b": "scott-optiplex-9030-aio"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


@dataclass
class FleetNode:
    """A single LMStudio endpoint on the network."""
    hostname: str
    base_url: str
    port: int
    label: str
    ip: str = ""
    models: list[str] = field(default_factory=list)
    healthy: bool = False
    last_healthy: float = 0.0
    max_ctx: int = 0
    supports_vision: bool = False
    supports_reasoning: bool = False
    latency_ms: float = 999999.0
    model_count: int = 0
    description: str = ""

    def is_available(self, age_seconds: float = 300.0) -> bool:
        if not self.healthy:
            return False
        return (time.time() - self.last_healthy) < age_seconds


class FleetProfile(ProviderProfile):
    """Fleet provider — routes to the best available LMStudio endpoint."""

    def __init__(self):
        super().__init__(
            name="fleet",
            aliases=("distributed", "network", "remote"),
            display_name="Fleet (Distributed Inference)",
            description="Route to the best available LMStudio endpoint on the network",
            signup_url="",
            auth_type="none",
            supports_health_check=True,
            default_aux_model="fleet",
        )

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **ctx: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {}


# Register the provider profile
fleet_profile = FleetProfile()
register_provider(fleet_profile)


# ---------------------------------------------------------------------------
# Fleet runtime (lazy-loaded to avoid circular imports)
# ---------------------------------------------------------------------------

_fleet: Optional[Any] = None
_fleet_config: dict = {}
_last_discover: float = 0.0
DISCOVER_COOLDOWN = 120  # seconds between discovery scans


def _ensure_fleet(config: dict | None = None) -> Any:
    """Get or create the fleet instance, triggering discovery if needed."""
    global _fleet, _fleet_config, _last_discover

    if config:
        _fleet_config.update(config)

    now = time.time()
    if _fleet is None or (now - _last_discover) > DISCOVER_COOLDOWN:
        _fleet = None  # force re-discovery
        _last_discover = now

    if _fleet is None:
        # Import here to avoid circular imports
        from agent.fleet import get_fleet

        fleet_cfg = _fleet_config.get("fleet", {})
        if not isinstance(fleet_cfg, dict):
            fleet_cfg = {}
        if not fleet_cfg.get("enabled", False):
            return None

        _fleet = get_fleet(config=fleet_cfg)
        asyncio.new_event_loop()  # ensure event loop exists
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        async def _do_discover():
            nodes = await _fleet.discover()
            if nodes:
                await _fleet.load_model_catalog(nodes)

        try:
            loop.run_until_complete(_do_discover())
        except Exception as exc:
            logger.debug("Fleet discovery failed: %s", exc)
            _fleet = None
            return None

    return _fleet


def get_fleet_nodes(config: dict | None = None) -> list[FleetNode]:
    """Get available fleet nodes. Discovers if needed."""
    fleet = _ensure_fleet(config)
    if fleet is None:
        return []
    return [n for n in fleet.nodes.values() if n.is_available(age_seconds=300)]


def get_fleet_model_catalog(config: dict | None = None) -> dict[str, list[str]]:
    """Get the full model catalog across all fleet nodes."""
    fleet = _ensure_fleet(config)
    if fleet is None:
        return {}
    return {n.hostname: n.models for n in fleet.nodes.values() if n.healthy}


def find_best_node_for_model(
    model: str,
    config: dict | None = None,
    prefer: str | None = None,
    avoid: list[str] | None = None,
) -> FleetNode | None:
    """Find the best fleet node for a specific model."""
    fleet = _ensure_fleet(config)
    if fleet is None:
        return None

    # Check for per-model routing override
    model_routing = _fleet_config.get("fleet", {}).get("model_routing", {})
    if isinstance(model_routing, dict):
        for pattern, target in model_routing.items():
            if pattern.lower() in model.lower() or model.lower() in pattern.lower():
                prefer = target
                break

    return fleet.route(
        model,
        prefer=prefer,
        avoid=avoid,
    )


def fleet_complete(
    model: str,
    messages: list[dict],
    config: dict | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Send a completion request to the best fleet node.

    Synchronous wrapper around the async fleet.complete().
    """
    import urllib.request

    fleet = _ensure_fleet(config)
    if fleet is None:
        raise RuntimeError("Fleet is not enabled or no nodes available")

    node = find_best_node_for_model(model, config)
    if not node:
        raise RuntimeError(f"No fleet node available for model '{model}'")

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    payload.update(kwargs)

    url = f"{node.base_url}/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_key = os.environ.get("HERMES_FLEET_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode())


def fleet_status(config: dict | None = None) -> dict:
    """Get fleet status summary."""
    fleet = _ensure_fleet(config)
    if fleet is None:
        return {"enabled": False, "nodes": [], "message": "Fleet not enabled or no nodes available"}

    healthy = [n for n in fleet.nodes.values() if n.is_available(age_seconds=300)]
    return {
        "enabled": True,
        "total_nodes": len(fleet.nodes),
        "healthy_nodes": len(healthy),
        "nodes": [
            {
                "hostname": n.hostname,
                "label": n.label,
                "base_url": n.base_url,
                "model_count": n.model_count,
                "latency_ms": round(n.latency_ms, 1),
                "models": n.models[:20],
            }
            for n in fleet.nodes.values()
        ],
    }
