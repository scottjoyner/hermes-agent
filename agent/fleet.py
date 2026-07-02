"""Fleet / distributed inference layer.

Discovers LMStudio (OpenAI-compatible) endpoints on the local tailnet,
catalogs their models, and routes inference requests to the best available
node. This lets a Hermes agent leverage faster/more-capable models on
other machines without manual configuration.

Usage::

    from agent.fleet import Fleet

    fleet = Fleet()
    await fleet.discover()       # scan the tailnet
    fleet.load_model_catalog()   # poll /v1/models on each node
    result = await fleet.complete("qwen3.6-35b", messages=msgs)  # auto-select best node
    result = await fleet.complete("qwen3.6-35b", messages=msgs, prefer="destroyer")  # force a node
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FleetNode:
    """A single LMStudio endpoint on the network."""
    hostname: str
    base_url: str
    port: int
    label: str  # human-friendly name (e.g. "destroyer")
    ip: str = ""
    # Model catalog (filled by load_model_catalog)
    models: list[str] = field(default_factory=list)
    # Health
    healthy: bool = False
    last_healthy: float = 0.0  # timestamp
    # Capabilities
    max_ctx: int = 0
    supports_vision: bool = False
    supports_reasoning: bool = False
    # Latency (ms)
    latency_ms: float = 999999.0
    # Metadata
    model_count: int = 0
    description: str = ""

    def is_available(self, age_seconds: float = 300.0) -> bool:
        """Return True if the node was checked recently enough."""
        if not self.healthy:
            return False
        return (time.time() - self.last_healthy) < age_seconds

    def __repr__(self) -> str:
        status = "healthy" if self.healthy else "unhealthy"
        return f"FleetNode({self.label} @ {self.base_url} [{status}, {self.model_count} models, {self.latency_ms:.0f}ms])"


@dataclass
class FleetRoute:
    """Result of routing a request to a specific node."""
    node: FleetNode
    model: str
    request_kwargs: dict[str, Any]
    extra_headers: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fleet manager
# ---------------------------------------------------------------------------

class Fleet:
    """Discover, catalog, and route to LMStudio endpoints on the network."""

    # Default tailnet DNS suffix for Hermes agents
    DEFAULT_TAILNET = "kipnerter.ts.net"

    # Hostnames we know about (populated from config / user hints)
    KNOWN_HOSTS: list[str] = []

    # Port range to probe on each host
    PORTS = [1234, 8000, 8080, 11434, 3000]

    def __init__(self, config: dict | None = None):
        self.nodes: dict[str, FleetNode] = {}  # keyed by hostname
        self.config = config or {}
        self.enabled = self.config.get("enabled", False)
        self.tailnet = self.config.get("tailnet", self.DEFAULT_TAILNET)
        self.discovery_hosts = list(self.config.get("discovery_hosts", self.KNOWN_HOSTS))
        self.max_latency_ms = self.config.get("max_latency_ms", 5000)
        self.health_ttl = self.config.get("health_ttl", 300)
        self._discovered = False
        self._cataloged = False

        # Load known hosts from config
        if not self.discovery_hosts:
            self.discovery_hosts = self._load_known_hosts()

    def _load_known_hosts(self) -> list[str]:
        """Load known hostnames from config and environment."""
        hosts = []
        # From config
        if "fleet" in self.config:
            fc = self.config["fleet"]
            if isinstance(fc, dict):
                hosts.extend(fc.get("known_hosts", []))
        # From env (comma-separated)
        env_hosts = os.environ.get("HERMES_FLEET_HOSTS", "").strip()
        if env_hosts:
            hosts.extend(h.strip() for h in env_hosts.split(",") if h.strip())
        # From TAILNET env
        env_tailnet = os.environ.get("HERMES_FLEET_TAILNET", "").strip()
        if env_tailnet:
            self.tailnet = env_tailnet
        # From TAILNET_DNS env
        env_dns = os.environ.get("HERMES_FLEET_TAILNET_DNS", "").strip()
        if env_dns:
            self.tailnet = env_dns
        return hosts

    async def discover(self, hosts: list[str] | None = None) -> list[FleetNode]:
        """Probe hosts for LMStudio endpoints.

        Returns the list of discovered nodes.
        """
        target_hosts = hosts or self.discovery_hosts
        if not target_hosts:
            # Try Tailscale DNS discovery
            target_hosts = await self._discover_tailnet_hosts()

        self.nodes = {}
        for hostname in target_hosts:
            for port in self.PORTS:
                node = await self._probe(hostname, port)
                if node:
                    self.nodes[hostname] = node
                    logger.info("Discovered LMStudio on %s:%d (%s, %d models)",
                                hostname, port, node.description, node.model_count)

        self._discovered = True
        return list(self.nodes.values())

    async def _discover_tailnet_hosts(self) -> list[str]:
        """Try to discover hosts via Tailscale DNS.

        Probe common Hermes agent hostnames on the tailnet.
        """
        hosts = set()
        # Add our known hosts
        hosts.update(self.discovery_hosts)
        # Try to resolve common tailnet names
        common_names = [
            "x1-370", "demo-1", "deathstar-XPS-8920", "scott-optiplex-9030-aio",
            "destroyer", "scotts-macbook-air", "scottsmacbookair", "macbook-air",
        ]
        for name in common_names:
            fqdn = f"{name}.{self.tailnet}"
            hosts.add(fqdn)
            hosts.add(name)  # also try bare hostname
        return list(hosts)

    async def _probe(self, hostname: str, port: int) -> FleetNode | None:
        """Probe a single host:port for LMStudio. Returns node if found."""
        base_url = f"http://{hostname}:{port}"
        label = hostname.split(".")[0] if "." in hostname else hostname

        node = FleetNode(
            hostname=hostname,
            base_url=base_url,
            port=port,
            label=label,
        )

        try:
            # Health check: try OpenAI-compatible /v1/models, then Ollama /api/tags
            import urllib.request
            elapsed = None
            data = None
            
            # Try OpenAI-compatible endpoint first
            for ep in ["/v1/models", "/api/v1/models"]:
                url = f"{base_url}{ep}"
                req = urllib.request.Request(url, method="GET")
                req.add_header("Accept", "application/json")
                start = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode())
                    elapsed = (time.time() - start) * 1000
                    if isinstance(data, dict) and "data" in data:
                        break
                except Exception:
                    continue
            
            # Fallback: try Ollama /api/tags
            if data is None or not (isinstance(data, dict) and "data" in data):
                url = f"{base_url}/api/tags"
                req = urllib.request.Request(url, method="GET")
                req.add_header("Accept", "application/json")
                start = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode())
                    elapsed = (time.time() - start) * 1000
                    # Ollama format: {"models": [...]}
                    if isinstance(data, dict) and "models" in data:
                        models = [m.get("name", m.get("id", "")) for m in data["models"] if isinstance(m, dict)]
                        node.models = models
                        node.healthy = True
                        node.last_healthy = time.time()
                        node.latency_ms = elapsed
                        node.model_count = len(models)
                        node.description = f"Ollama-compatible ({len(models)} models)"
                        return node
                except Exception:
                    pass
            
            if elapsed is None:
                return None

            if isinstance(data, dict) and "data" in data:
                models = [m["id"] for m in data["data"] if isinstance(m, dict) and "id" in m]
                node.models = models
                node.model_count = len(models)
                node.healthy = True
                node.last_healthy = time.time()
                node.latency_ms = elapsed
                node.description = f"{len(models)} models"
            elif isinstance(data, list) and data:
                # Some servers return models directly
                models = [m.get("id", m) for m in data if isinstance(m, dict)]
                node.models = models
                node.model_count = len(models)
                node.healthy = True
                node.last_healthy = time.time()
                node.latency_ms = elapsed
                node.description = f"{len(models)} models"
            else:
                node.description = "unknown response"
        except Exception as exc:
            logger.debug("Probe %s:%d failed: %s", hostname, port, exc)
            node.description = f"unreachable ({exc})"

        return node if node.healthy else None

    async def load_model_catalog(self, nodes: list[FleetNode] | None = None) -> dict[str, list[str]]:
        """Poll /v1/models on each node. Returns {hostname: [model_ids]}.

        Call this after discover() to get full model lists.
        """
        targets = nodes or (list(self.nodes.values()) if self._discovered else [])
        if not targets:
            await self.discover()
            targets = list(self.nodes.values())

        catalog = {}
        for node in targets:
            if node.healthy:
                catalog[node.hostname] = node.models
                self._cataloged = True

        return catalog

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route(
        self,
        model: str,
        *,
        prefer: str | None = None,
        avoid: list[str] | None = None,
        need_vision: bool = False,
        need_reasoning: bool = False,
        cheap: bool = False,
    ) -> FleetNode | None:
        """Select the best node for a given model.

        Args:
            model: Model ID to look for (partial match accepted).
            prefer: Hostname or label to prefer.
            avoid: Hostnames to skip.
            need_vision: Require a vision-capable model.
            need_reasoning: Require a reasoning-capable model.
            cheap: Prefer the fastest/cheapest available node.
        """
        avoid = avoid or []
        candidates = []

        for node in self.nodes.values():
            if not node.is_available(age_seconds=self.health_ttl):
                continue
            if node.hostname in avoid or node.label in avoid:
                continue

            # Check model match
            model_match = self._model_matches(model, node.models)
            if not model_match and not cheap:
                continue

            # Check capabilities
            if need_vision and not node.supports_vision:
                continue
            if need_reasoning and not node.supports_reasoning:
                continue

            candidates.append((node, model_match))

        if not candidates:
            return None

        # Sort: prefer > latency > model match score
        def score(item):
            node, mmatch = item
            s = (0 if mmatch else 1) * 1_000_000 + int(node.latency_ms)
            if prefer and (node.hostname == prefer or node.label == prefer):
                s -= 500_000  # huge bonus for preferred node
            if cheap:
                s -= int(node.latency_ms)  # prefer fast nodes for cheap tasks
            return s

        candidates.sort(key=score)
        return candidates[0][0] if candidates else None

    def _model_matches(self, model: str, available: list[str]) -> bool:
        """Check if the requested model matches any available model."""
        model_lower = model.lower().strip()
        for avail in available:
            avail_lower = avail.lower()
            if model_lower == avail_lower:
                return True
            # Partial match: "qwen3.6-35b" matches "qwen3.6-35b" or "qwen3.6-35b:latest"
            if model_lower in avail_lower or avail_lower.replace(":latest", "").replace(":instruct", "") in model_lower:
                return True
        return False

    # ------------------------------------------------------------------
    # Inference proxy
    # ------------------------------------------------------------------

    async def complete(
        self,
        model: str,
        messages: list[dict],
        *,
        prefer: str | None = None,
        avoid: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat.completions request to the best fleet node.

        Returns the parsed JSON response from the remote LMStudio instance.
        """
        node = self.route(model, prefer=prefer, avoid=avoid)
        if not node:
            raise RuntimeError(f"No fleet node available for model '{model}'. Run discover() first.")

        # Build the request
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        payload.update(kwargs)

        # Send via HTTP (try both endpoints)
        for ep in ["/v1/chat/completions", "/api/v1/chat/completions"]:
            url = f"{node.base_url}{ep}"
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            # Forward API key if configured
            api_key = os.environ.get("HERMES_FLEET_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")

            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    return json.loads(resp.read().decode())
            except Exception as exc:
                logger.error("Fleet request to %s (%s) failed: %s", node.hostname, ep, exc)
                # Try next best node
                next_node = self._route_with_avoid(model, [node.hostname], prefer=prefer)
                if next_node:
                    return await self._complete_on_node(next_node, model, messages, **kwargs)
                raise

    def _route_with_avoid(
        self, model: str, avoid: list[str], prefer: str | None = None
    ) -> FleetNode | None:
        """Route with existing avoid list merged in."""
        existing = []
        for n in self.nodes.values():
            if n.is_available(age_seconds=self.health_ttl):
                existing.append(n)
        temp = Fleet()
        temp.nodes = {n.hostname: n for n in existing}
        return temp.route(model, prefer=prefer, avoid=avoid)  # type: ignore[call-arg]

    async def _complete_on_node(
        self, node: FleetNode, model: str, messages: list[dict], **kwargs: Any
    ) -> dict[str, Any]:
        """Send a request directly to a specific node."""
        url = f"{node.base_url}/v1/chat/completions"
        # Try both endpoints
        for ep in ["/v1/chat/completions", "/api/v1/chat/completions"]:
            url = f"{node.base_url}{ep}"
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            payload.update(kwargs)

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            api_key = os.environ.get("HERMES_FLEET_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")

            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    return json.loads(resp.read().decode())
            except Exception as e:
                if ep == "/v1/chat/completions":
                    continue  # Try the fallback endpoint
                raise

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return fleet status summary."""
        healthy = [n for n in self.nodes.values() if n.is_available(age_seconds=self.health_ttl)]
        return {
            "enabled": self.enabled,
            "discovered": self._discovered,
            "cataloged": self._cataloged,
            "total_nodes": len(self.nodes),
            "healthy_nodes": len(healthy),
            "nodes": [
                {
                    "hostname": n.hostname,
                    "label": n.label,
                    "base_url": n.base_url,
                    "healthy": n.healthy,
                    "model_count": n.model_count,
                    "latency_ms": round(n.latency_ms, 1),
                    "models": n.models[:20],  # first 20
                }
                for n in self.nodes.values()
            ],
        }

    def __repr__(self) -> str:
        s = self.status()
        return f"Fleet({s['healthy_nodes']}/{s['total_nodes']} healthy, {s['cataloged'] and 'cataloged' or 'not cataloged'})"


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

# Singleton — lazily initialized
_fleet_instance: Fleet | None = None


def get_fleet(config: dict | None = None) -> Fleet:
    """Get the global fleet singleton. Creates it if needed."""
    global _fleet_instance
    if _fleet_instance is None:
        _fleet_instance = Fleet(config=config)
    return _fleet_instance


async def discover_fleet(config: dict | None = None) -> list[FleetNode]:
    """Convenience: discover and catalog fleet nodes."""
    fleet = get_fleet(config)
    nodes = await fleet.discover()
    await fleet.load_model_catalog(nodes)
    return nodes


async def fleet_complete(
    model: str,
    messages: list[dict],
    config: dict | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Convenience: send a completion request to the best fleet node."""
    fleet = get_fleet(config)
    if not fleet._discovered:
        await fleet.discover()
    if not fleet._cataloged:
        await fleet.load_model_catalog()
    return await fleet.complete(model, messages, **kwargs)
