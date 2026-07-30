"""Configuration, health probing, and route scoring for the Hermes fleet proxy."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import threading
import time
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from hermes_cli.urllib_security import open_credentialed_url

_CACHE_PREFIX = "x-hermes-cache-"
_HOP_HEADERS = {
    "authorization", "connection", "content-length", "host", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade",
}
_AFFINITY_LIMIT = 4096


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def integer(value: Any, default: int = 0, minimum: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def floating(value: Any, default: float = 0.0, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def normalize_base_url(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return raw


def is_private_endpoint(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in {"localhost", "host.docker.internal"}:
        return True
    if "." not in host and ":" not in host:
        return True
    if host.endswith((".lan", ".local", ".internal", ".ts.net")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def join_endpoint(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    suffix = "/" + str(path or "").lstrip("/")
    if base.lower().endswith("/v1") and suffix.lower().startswith("/v1/"):
        suffix = suffix[3:]
    return base + suffix


def model_key(value: str) -> str:
    result = str(value or "").strip().lower()
    for suffix in (":latest", ":instruct"):
        if result.endswith(suffix):
            result = result[: -len(suffix)]
    return result


def estimate_tokens(value: Any) -> int:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    return max(1, math.ceil(len(text) / 3.5))


def request_has_vision(payload: Mapping[str, Any]) -> bool:
    stack: list[Any] = [payload.get("messages"), payload.get("input")]
    while stack:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, Mapping):
            if str(value.get("type") or "").lower() in {
                "image", "image_url", "input_image",
            } or "image_url" in value:
                return True
            stack.extend(value.values())
    return False


@dataclass(frozen=True)
class FleetNodeConfig:
    name: str
    base_url: str
    provider: str = "openai-compatible"
    allow_remote: bool = False
    api_key_env: str = ""
    context_length: int = 0
    max_concurrency: int = 1
    priority: float = 0.0
    prefill_tps: float = 0.0
    decode_tps: float = 0.0
    supports_tools: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = True
    accept_unknown_models: bool = False
    models: tuple[str, ...] = ()
    model_map: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    probe_timeout: float = 2.0
    request_timeout: float = 600.0

    @property
    def aliases(self) -> dict[str, str]:
        return dict(self.model_map)

    @property
    def static_headers(self) -> dict[str, str]:
        return dict(self.headers)


@dataclass(frozen=True)
class FleetConfig:
    enabled: bool
    nodes: tuple[FleetNodeConfig, ...]
    health_ttl: float = 30.0
    default_max_output: int = 4096
    max_request_bytes: int = 64 * 1024 * 1024
    max_attempts: int = 2
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    allow_non_loopback: bool = False
    listen_token_env: str = ""


def parse_fleet_config(root: Mapping[str, Any]) -> FleetConfig:
    section = mapping(root.get("fleet"))
    raw_nodes = section.get("nodes")
    nodes: list[FleetNodeConfig] = []
    if isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes)):
        for index, raw in enumerate(raw_nodes):
            item = mapping(raw)
            base_url = normalize_base_url(item.get("base_url"))
            name = str(item.get("name") or f"node-{index + 1}").strip()
            allow_remote = bool(item.get("allow_remote", False))
            if not name or not base_url or (not allow_remote and not is_private_endpoint(base_url)):
                continue
            raw_models = item.get("models")
            models = tuple(
                str(model).strip() for model in raw_models if str(model).strip()
            ) if isinstance(raw_models, Sequence) and not isinstance(raw_models, (str, bytes)) else ()
            aliases = tuple(
                (str(key).strip(), str(value).strip())
                for key, value in mapping(item.get("model_map")).items()
                if str(key).strip() and str(value).strip()
            )
            headers = tuple(
                (str(key).strip(), str(value))
                for key, value in mapping(item.get("headers")).items()
                if str(key).strip().lower() not in _HOP_HEADERS
            )
            if not bool(item.get("enabled", True)):
                continue
            nodes.append(FleetNodeConfig(
                name=name,
                base_url=base_url,
                provider=str(item.get("provider") or "openai-compatible").strip(),
                allow_remote=allow_remote,
                api_key_env=str(item.get("api_key_env") or "").strip(),
                context_length=integer(item.get("context_length")),
                max_concurrency=max(1, integer(item.get("max_concurrency"), 1, 1)),
                priority=floating(item.get("priority")),
                prefill_tps=floating(item.get("prefill_tokens_per_second")),
                decode_tps=floating(item.get("decode_tokens_per_second")),
                supports_tools=bool(item.get("supports_tools", True)),
                supports_vision=bool(item.get("supports_vision", False)),
                supports_reasoning=bool(item.get("supports_reasoning", True)),
                accept_unknown_models=bool(item.get("accept_unknown_models", False)),
                models=models,
                model_map=aliases,
                headers=headers,
                probe_timeout=min(10.0, max(0.1, floating(item.get("probe_timeout_seconds"), 2.0))),
                request_timeout=min(3600.0, max(1.0, floating(item.get("request_timeout_seconds"), 600.0))),
            ))
    listen = mapping(section.get("listen"))
    return FleetConfig(
        enabled=bool(section.get("enabled", False)),
        nodes=tuple(nodes),
        health_ttl=min(300.0, max(1.0, floating(section.get("health_ttl_seconds"), 30.0))),
        default_max_output=max(1, integer(section.get("default_max_output_tokens"), 4096, 1)),
        max_request_bytes=max(1024, integer(section.get("max_request_bytes"), 64 * 1024 * 1024, 1024)),
        max_attempts=min(5, max(1, integer(section.get("max_attempts"), 2, 1))),
        listen_host=str(listen.get("host") or "127.0.0.1").strip(),
        listen_port=min(65535, max(1, integer(listen.get("port"), 8765, 1))),
        allow_non_loopback=bool(listen.get("allow_non_loopback", False)),
        listen_token_env=str(listen.get("token_env") or "").strip(),
    )


@dataclass
class NodeState:
    config: FleetNodeConfig
    healthy: bool = False
    models: tuple[str, ...] = ()
    latency_ms: float = 1_000_000.0
    latency_ema_ms: float = 1_000_000.0
    last_probe: float = 0.0
    last_error: str = ""
    inflight: int = 0
    successes: int = 0
    failures: int = 0

    def available_models(self) -> tuple[str, ...]:
        return self.models or self.config.models


@dataclass(frozen=True)
class RouteRequirements:
    model: str
    input_tokens: int
    max_output_tokens: int
    needs_tools: bool = False
    needs_vision: bool = False
    needs_reasoning: bool = False
    session_id: str = ""
    checkpoint_id: str = ""

    @property
    def required_context(self) -> int:
        total = max(0, self.input_tokens) + max(0, self.max_output_tokens)
        return total + max(256, math.ceil(total * 0.05))


@dataclass(frozen=True)
class RouteDecision:
    node_name: str
    upstream_model: str
    score: float
    required_context: int
    context_headroom: int
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def requirements_from_payload(
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    default_max_output: int = 4096,
) -> RouteRequirements:
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    output = integer(
        payload.get("max_completion_tokens") or payload.get("max_tokens"),
        default_max_output,
        1,
    )
    content = payload.get("messages") if payload.get("messages") is not None else payload.get("input")
    reasoning = payload.get("reasoning") or payload.get("reasoning_effort")
    needs_reasoning = reasoning.get("enabled") is not False if isinstance(reasoning, Mapping) else bool(reasoning)
    return RouteRequirements(
        model=str(payload.get("model") or "").strip(),
        input_tokens=estimate_tokens(content),
        max_output_tokens=output,
        needs_tools=bool(payload.get("tools")),
        needs_vision=request_has_vision(payload),
        needs_reasoning=needs_reasoning,
        session_id=lowered.get("x-hermes-cache-session-id") or lowered.get("x-hermes-session-id", ""),
        checkpoint_id=lowered.get("x-hermes-cache-checkpoint-id", ""),
    )


class FleetRouter:
    def __init__(self, config: FleetConfig):
        self.config = config
        self._lock = threading.RLock()
        self._nodes = {node.name: NodeState(node) for node in config.nodes}
        self._sessions: OrderedDict[str, str] = OrderedDict()
        self._checkpoints: OrderedDict[str, str] = OrderedDict()

    def upstream_headers(self, node: FleetNodeConfig, incoming: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "hermes-fleet-router/0.1",
        }
        for key, value in (incoming or {}).items():
            if str(key).lower().startswith(_CACHE_PREFIX):
                headers[str(key)] = str(value)
        headers.update(node.static_headers)
        if node.api_key_env:
            token = os.getenv(node.api_key_env, "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _model_urls(self, node: FleetNodeConfig) -> list[str]:
        urls = [join_endpoint(node.base_url, "/v1/models")]
        if not node.base_url.lower().endswith("/v1"):
            urls.append(node.base_url + "/models")
        if node.provider.lower() == "ollama" or node.base_url.endswith(":11434"):
            urls.append(node.base_url.removesuffix("/v1") + "/api/tags")
        return list(dict.fromkeys(urls))

    def _probe(self, node: FleetNodeConfig) -> tuple[bool, tuple[str, ...], float, str]:
        error = ""
        for url in self._model_urls(node):
            request = urllib.request.Request(url, method="GET")
            for key, value in self.upstream_headers(node).items():
                if key.lower() != "content-type":
                    request.add_header(key, value)
            started = time.monotonic()
            try:
                with open_credentialed_url(request, timeout=node.probe_timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                latency = (time.monotonic() - started) * 1000
                items = payload.get("data") if isinstance(payload, Mapping) else None
                if not isinstance(items, list) and isinstance(payload, Mapping):
                    items = payload.get("models")
                if isinstance(items, list):
                    models = tuple(
                        str(item.get("id") or item.get("name") or item.get("model") or "").strip()
                        for item in items if isinstance(item, Mapping)
                        and str(item.get("id") or item.get("name") or item.get("model") or "").strip()
                    )
                    return True, models or node.models, latency, ""
                error = f"unexpected models response from {url}"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        return False, node.models, 1_000_000.0, error or "unreachable"

    def refresh(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            targets = [state.config for state in self._nodes.values()
                       if force or now - state.last_probe >= self.config.health_ttl]
        if targets:
            results = {}
            with ThreadPoolExecutor(max_workers=min(32, len(targets)), thread_name_prefix="fleet-probe") as pool:
                futures = {pool.submit(self._probe, node): node.name for node in targets}
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        results[name] = future.result()
                    except Exception as exc:
                        results[name] = (False, (), 1_000_000.0, f"{type(exc).__name__}: {exc}")
            with self._lock:
                checked = time.time()
                for name, (healthy, models, latency, error) in results.items():
                    state = self._nodes[name]
                    state.healthy, state.models = healthy, models
                    state.latency_ms, state.last_error, state.last_probe = latency, error, checked
                    if healthy:
                        state.latency_ema_ms = latency if state.latency_ema_ms >= 1_000_000 else state.latency_ema_ms * 0.75 + latency * 0.25
        return self.status()

    def _resolved_model(self, requested: str, state: NodeState) -> tuple[str, bool] | None:
        for alias, upstream in state.config.model_map:
            if model_key(alias) == model_key(requested):
                return upstream, True
        for available in state.available_models():
            if model_key(available) == model_key(requested):
                return available, True
        return (requested, False) if state.config.accept_unknown_models else None

    def rank(self, requirements: RouteRequirements, exclude: Sequence[str] = ()) -> list[RouteDecision]:
        now, excluded = time.time(), set(exclude)
        decisions = []
        with self._lock:
            session_node = self._sessions.get(requirements.session_id, "")
            checkpoint_node = self._checkpoints.get(requirements.checkpoint_id, "")
            for state in self._nodes.values():
                node = state.config
                if node.name in excluded or not state.healthy or now - state.last_probe > self.config.health_ttl * 2:
                    continue
                if requirements.needs_tools and not node.supports_tools:
                    continue
                if requirements.needs_vision and not node.supports_vision:
                    continue
                if requirements.needs_reasoning and not node.supports_reasoning:
                    continue
                resolved = self._resolved_model(requirements.model, state)
                if resolved is None:
                    continue
                upstream_model, exact = resolved
                required = requirements.required_context
                if node.context_length and required > node.context_length:
                    continue
                headroom = max(0, node.context_length - required) if node.context_length else 0
                score = 100 * (headroom / node.context_length if node.context_length else 0.5)
                score += 30 if exact else 0
                score += node.priority * 10
                score += math.log2(max(1.0, node.prefill_tps)) * 3
                score += math.log2(max(1.0, node.decode_tps)) * 2
                score -= min(100, state.latency_ema_ms / 20)
                score -= (state.inflight / max(1, node.max_concurrency)) * 80
                reasons = [f"headroom={headroom if node.context_length else 'unknown'}",
                           f"load={state.inflight}/{node.max_concurrency}",
                           f"latency_ema_ms={state.latency_ema_ms:.1f}"]
                if exact:
                    reasons.append("model=exact")
                if session_node == node.name:
                    score += 80
                    reasons.append("session-affinity")
                if checkpoint_node == node.name:
                    score += 140
                    reasons.append("checkpoint-affinity")
                decisions.append(RouteDecision(node.name, upstream_model, round(score, 4), required, headroom, tuple(reasons)))
        return sorted(decisions, key=lambda item: (-item.score, item.node_name))

    def route(self, requirements: RouteRequirements, exclude: Sequence[str] = ()) -> RouteDecision | None:
        ranked = self.rank(requirements, exclude)
        return ranked[0] if ranked else None

    def acquire(self, decision: RouteDecision) -> FleetNodeConfig:
        with self._lock:
            self._nodes[decision.node_name].inflight += 1
            return self._nodes[decision.node_name].config

    def _remember(self, values: OrderedDict[str, str], key: str, node: str) -> None:
        if key:
            values[key] = node
            values.move_to_end(key)
            while len(values) > _AFFINITY_LIMIT:
                values.popitem(last=False)

    def release(self, decision: RouteDecision, success: bool, duration_ms: float,
                session_id: str = "", checkpoint_id: str = "", error: str = "") -> None:
        with self._lock:
            state = self._nodes[decision.node_name]
            state.inflight = max(0, state.inflight - 1)
            if success:
                state.successes += 1
                state.last_error = ""
                state.latency_ema_ms = duration_ms if state.latency_ema_ms >= 1_000_000 else state.latency_ema_ms * 0.8 + duration_ms * 0.2
                self._remember(self._sessions, session_id, decision.node_name)
                self._remember(self._checkpoints, checkpoint_id, decision.node_name)
            else:
                state.failures += 1
                state.last_error = error

    def all_models(self) -> list[str]:
        with self._lock:
            values = {model for state in self._nodes.values() for model in state.available_models()}
            values.update(alias for state in self._nodes.values() for alias, _ in state.config.model_map)
        return sorted(values)

    def status(self) -> dict[str, Any]:
        with self._lock:
            nodes = [{
                "name": state.config.name,
                "base_url": state.config.base_url,
                "provider": state.config.provider,
                "healthy": state.healthy,
                "models": list(state.available_models()),
                "context_length": state.config.context_length,
                "max_concurrency": state.config.max_concurrency,
                "inflight": state.inflight,
                "latency_ms": round(state.latency_ms, 1),
                "latency_ema_ms": round(state.latency_ema_ms, 1),
                "successes": state.successes,
                "failures": state.failures,
                "last_error": state.last_error,
                "last_probe": state.last_probe,
            } for state in self._nodes.values()]
        return {
            "enabled": self.config.enabled,
            "configured_nodes": len(nodes),
            "healthy_nodes": sum(1 for node in nodes if node["healthy"]),
            "listen": f"http://{self.config.listen_host}:{self.config.listen_port}/v1",
            "nodes": nodes,
        }
