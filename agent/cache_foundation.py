"""Engine-neutral prefix-cache manifests, affinity state, and route scoring.

This module deliberately does not serialize model KV tensors. It defines the
stable contract between Hermes and an inference server or cache-aware proxy:

* deterministic request and checkpoint identifiers;
* local-endpoint eligibility and safe routing headers;
* durable session affinity, checkpoint inventory, and request telemetry;
* a backend-neutral route scoring model.

Only hashes and operational metadata are persisted. Prompt text, tool payloads,
API keys, and provider responses are never written to the cache database.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from hermes_constants import get_hermes_home

CACHE_SCHEMA_VERSION = "hermes.cache.v1"
_HEADER_PREFIX = "X-Hermes-Cache-"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_DB_LOCK = threading.RLock()
_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def _canonical_json(value: Any) -> str:
    """Return stable JSON while preserving list order."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def hash_text(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def hash_json(value: Any) -> str:
    return hash_text(_canonical_json(value))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, Mapping):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(part.get("content"), str):
                parts.append(str(part["content"]))
        else:
            parts.append(str(part))
    return "".join(parts)


def _decorated_static_prefix(content: Any) -> str:
    """Extract Hermes' independently decorated static system part."""

    if not isinstance(content, list) or not content:
        return ""
    first = content[0]
    if not isinstance(first, Mapping):
        return ""
    marker = first.get("cache_control")
    text = first.get("text")
    if isinstance(marker, Mapping) and isinstance(text, str) and text:
        return text
    return ""


def _request_messages(request: Mapping[str, Any]) -> list[Any]:
    messages = request.get("messages")
    if isinstance(messages, list):
        return messages
    items = request.get("input")
    return items if isinstance(items, list) else []


def _system_message(messages: Sequence[Any]) -> Mapping[str, Any] | None:
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if str(message.get("role") or "").lower() in {"system", "developer"}:
            return message
    return None


def _prefix_messages(messages: Sequence[Any]) -> list[Any]:
    """Return the replay prefix before the newest conversational item."""

    return list(messages if len(messages) <= 1 else messages[:-1])


def _parsed_endpoint(base_url: str):
    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        return urlparse(raw if "://" in raw else f"http://{raw}")
    except ValueError:
        return None


def _normalize_base_url(base_url: str) -> str:
    parsed = _parsed_endpoint(base_url)
    if parsed is None:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    try:
        port_value = parsed.port
    except ValueError:
        return ""
    port = f":{port_value}" if port_value else ""
    path = (parsed.path or "").rstrip("/")
    return f"{parsed.scheme or 'http'}://{host}{port}{path}"


def classify_engine(provider: str, base_url: str) -> str:
    configured = os.getenv("HERMES_CACHE_ENGINE_ID", "").strip()
    if configured:
        return configured

    provider_name = str(provider or "").strip().lower()
    endpoint = str(base_url or "").lower()
    if provider_name == "lmstudio" or "lmstudio" in endpoint:
        return "lmstudio-managed"
    if provider_name == "ollama" or endpoint.rstrip("/").endswith(":11434"):
        return "ollama-managed"
    if _truthy(os.getenv("HERMES_CACHE_LLAMA_CPP")):
        return "llama.cpp"
    return provider_name or "openai-compatible"


def is_cache_eligible_endpoint(
    base_url: str,
    provider: str = "",
    *,
    allow_remote: bool | None = None,
) -> bool:
    """Return whether cache identifiers may be disclosed to an endpoint.

    ``provider`` is retained for API compatibility but is intentionally not a
    trust signal. A public URL does not become private merely because its
    configured provider name is ``lmstudio`` or ``ollama``.
    """

    del provider
    if allow_remote is None:
        allow_remote = _truthy(os.getenv("HERMES_CACHE_ALLOW_REMOTE"))
    if allow_remote:
        return True

    parsed = _parsed_endpoint(base_url)
    if parsed is None:
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in {"localhost", "host.docker.internal"}:
        return True
    if "." not in host and ":" not in host:
        return True
    if host.endswith((".local", ".lan", ".internal", ".ts.net")):
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return isinstance(ip, ipaddress.IPv4Address) and ip in _TAILSCALE_NETWORK


@dataclass(frozen=True)
class PromptCacheManifest:
    schema_version: str
    checkpoint_id: str
    request_prefix_id: str
    system_hash: str
    static_prefix_hash: str
    tool_schema_hash: str
    model: str
    model_fingerprint: str
    provider: str
    api_mode: str
    engine_id: str
    engine_fingerprint: str
    chat_template_hash: str
    kv_format: str
    endpoint: str
    session_id: str
    system_chars: int
    prefix_chars: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_manifest(
    request: Mapping[str, Any],
    *,
    session_id: str = "",
    model: str = "",
    provider: str = "",
    api_mode: str = "",
    base_url: str = "",
    stable_system_prefix: str = "",
) -> PromptCacheManifest:
    """Build an exact, deterministic cache manifest from provider kwargs.

    ``stable_system_prefix`` is an optional bridge for callers that know
    Hermes' stable system tier before provider decoration. Without it, the
    function uses a decorated static block when present and otherwise falls
    back conservatively to the full system message.
    """

    effective_model = str(request.get("model") or model or "")
    messages = _request_messages(request)
    system = _system_message(messages)
    system_content = system.get("content") if system else ""
    system_text = _content_text(system_content)
    decorated_static = _decorated_static_prefix(system_content)
    static_text = stable_system_prefix or decorated_static or system_text
    tools = request.get("tools") if isinstance(request.get("tools"), list) else []
    prefix_messages = _prefix_messages(messages)

    engine_id = classify_engine(provider, base_url)
    model_fingerprint = (
        os.getenv("HERMES_CACHE_MODEL_FINGERPRINT", "").strip()
        or effective_model
    )
    engine_fingerprint = (
        os.getenv("HERMES_CACHE_ENGINE_FINGERPRINT", "").strip()
        or engine_id
    )
    chat_template_hash = (
        os.getenv("HERMES_CACHE_CHAT_TEMPLATE_HASH", "").strip()
        or "unknown"
    )
    kv_format = os.getenv("HERMES_CACHE_KV_FORMAT", "").strip() or "managed"
    endpoint = _normalize_base_url(base_url)

    static_hash = hash_text(static_text)
    system_hash = hash_text(system_text)
    tools_hash = hash_json(tools)
    checkpoint_id = hash_json(
        {
            "schema": CACHE_SCHEMA_VERSION,
            "model_fingerprint": model_fingerprint,
            "engine_fingerprint": engine_fingerprint,
            "chat_template_hash": chat_template_hash,
            "kv_format": kv_format,
            "static_prefix_hash": static_hash,
            "tool_schema_hash": tools_hash,
            "api_mode": api_mode,
        }
    )
    request_prefix_id = hash_json(
        {
            "checkpoint_id": checkpoint_id,
            "messages": prefix_messages,
            "tools": tools,
        }
    )

    if stable_system_prefix:
        source = "runtime-static"
    elif decorated_static:
        source = "decorated-static"
    else:
        source = "full-system"

    return PromptCacheManifest(
        schema_version=CACHE_SCHEMA_VERSION,
        checkpoint_id=checkpoint_id,
        request_prefix_id=request_prefix_id,
        system_hash=system_hash,
        static_prefix_hash=static_hash,
        tool_schema_hash=tools_hash,
        model=effective_model,
        model_fingerprint=model_fingerprint,
        provider=str(provider or ""),
        api_mode=str(api_mode or ""),
        engine_id=engine_id,
        engine_fingerprint=engine_fingerprint,
        chat_template_hash=chat_template_hash,
        kv_format=kv_format,
        endpoint=endpoint,
        session_id=str(session_id or ""),
        system_chars=len(system_text),
        prefix_chars=(
            len(_canonical_json(prefix_messages)) + len(_canonical_json(tools))
        ),
        source=source,
    )


def build_cache_headers(manifest: PromptCacheManifest) -> dict[str, str]:
    values = {
        "Schema": manifest.schema_version,
        "Checkpoint-Id": manifest.checkpoint_id,
        "Prefix-Id": manifest.request_prefix_id,
        "System-Hash": manifest.system_hash,
        "Static-Hash": manifest.static_prefix_hash,
        "Tool-Hash": manifest.tool_schema_hash,
        "Engine": manifest.engine_id,
        "Mode": os.getenv("HERMES_CACHE_MODE", "prefer").strip() or "prefer",
    }
    if manifest.session_id:
        values["Session-Id"] = manifest.session_id
    return {
        f"{_HEADER_PREFIX}{key}": value
        for key, value in values.items()
        if value
    }


@dataclass(frozen=True)
class CacheRouteCandidate:
    endpoint: str
    healthy: bool = True
    session_affinity: bool = False
    checkpoint_present: bool = False
    model_loaded: bool = False
    queue_depth: int = 0
    prefill_tokens_per_second: float = 0.0
    estimated_prefix_tokens: int = 0
    latency_ms: float = 0.0
    cold_load_ms: float = 0.0


def route_score(candidate: CacheRouteCandidate) -> float:
    """Return a lower-is-better cost where cache locality dominates latency."""

    if not candidate.healthy:
        return float("inf")

    score = max(0.0, candidate.latency_ms)
    score += max(0, candidate.queue_depth) * 1_000.0
    if not candidate.model_loaded:
        score += max(0.0, candidate.cold_load_ms) or 60_000.0
    if not candidate.checkpoint_present:
        tps = max(0.1, candidate.prefill_tokens_per_second)
        score += (
            max(0, candidate.estimated_prefix_tokens) / tps * 1_000.0
        )
    else:
        score -= 100_000.0
    if candidate.model_loaded:
        score -= 10_000.0
    if candidate.session_affinity:
        score -= 1_000_000.0
    return score


def select_best_route(
    candidates: Iterable[CacheRouteCandidate],
) -> CacheRouteCandidate | None:
    viable = [candidate for candidate in candidates if candidate.healthy]
    if not viable:
        return None
    return min(
        viable,
        key=lambda candidate: (route_score(candidate), candidate.endpoint),
    )


class CacheStateStore:
    """Durable SQLite store for affinity, inventory, and cache telemetry."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path)
            if path
            else get_hermes_home() / "cache" / "state.db"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with _DB_LOCK, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_affinity (
                    session_id TEXT PRIMARY KEY,
                    endpoint TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    request_prefix_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    model TEXT NOT NULL,
                    engine_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    prefix_tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (checkpoint_id, endpoint)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    api_request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    model TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    request_prefix_id TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_requests_session_created
                    ON requests(session_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_updated
                    ON checkpoints(updated_at DESC);
                """
            )

    def bind_session(self, manifest: PromptCacheManifest) -> None:
        if not manifest.session_id or not manifest.endpoint:
            return
        with _DB_LOCK, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_affinity(
                    session_id, endpoint, provider, model, checkpoint_id,
                    request_prefix_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    endpoint=excluded.endpoint,
                    provider=excluded.provider,
                    model=excluded.model,
                    checkpoint_id=excluded.checkpoint_id,
                    request_prefix_id=excluded.request_prefix_id,
                    updated_at=excluded.updated_at
                """,
                (
                    manifest.session_id,
                    manifest.endpoint,
                    manifest.provider,
                    manifest.model,
                    manifest.checkpoint_id,
                    manifest.request_prefix_id,
                    time.time(),
                ),
            )

    def affinity(self, session_id: str) -> dict[str, Any] | None:
        with _DB_LOCK, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_affinity WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def register_checkpoint(
        self,
        manifest: PromptCacheManifest,
        *,
        state: str = "observed",
        prefix_tokens: int = 0,
    ) -> None:
        if not manifest.endpoint:
            return
        with _DB_LOCK, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(
                    checkpoint_id, endpoint, model, engine_id, state,
                    prefix_tokens, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(checkpoint_id, endpoint) DO UPDATE SET
                    model=excluded.model,
                    engine_id=excluded.engine_id,
                    state=excluded.state,
                    prefix_tokens=MAX(
                        checkpoints.prefix_tokens,
                        excluded.prefix_tokens
                    ),
                    updated_at=excluded.updated_at
                """,
                (
                    manifest.checkpoint_id,
                    manifest.endpoint,
                    manifest.model,
                    manifest.engine_id,
                    state,
                    max(0, int(prefix_tokens or 0)),
                    time.time(),
                ),
            )

    def record_request(
        self,
        *,
        api_request_id: str,
        manifest: PromptCacheManifest,
        duration_ms: int,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        error: str = "",
    ) -> None:
        request_id = str(api_request_id or "").strip()
        if not request_id:
            request_id = hash_json(
                {
                    "session": manifest.session_id,
                    "prefix": manifest.request_prefix_id,
                    "time_ns": time.time_ns(),
                }
            )
        with _DB_LOCK, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO requests(
                    api_request_id, session_id, endpoint, model, checkpoint_id,
                    request_prefix_id, duration_ms, success, prompt_tokens,
                    completion_tokens, cache_read_tokens, cache_write_tokens,
                    error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    manifest.session_id,
                    manifest.endpoint,
                    manifest.model,
                    manifest.checkpoint_id,
                    manifest.request_prefix_id,
                    max(0, int(duration_ms)),
                    1 if success else 0,
                    max(0, int(prompt_tokens or 0)),
                    max(0, int(completion_tokens or 0)),
                    max(0, int(cache_read_tokens or 0)),
                    max(0, int(cache_write_tokens or 0)),
                    str(error or "")[:1000],
                    time.time(),
                ),
            )

    def summary(self) -> dict[str, Any]:
        with _DB_LOCK, self._connect() as connection:
            affinity_count = connection.execute(
                "SELECT COUNT(*) FROM session_affinity"
            ).fetchone()[0]
            checkpoint_count = connection.execute(
                "SELECT COUNT(*) FROM checkpoints"
            ).fetchone()[0]
            request_row = connection.execute(
                """
                SELECT COUNT(*) AS requests,
                       COALESCE(SUM(success), 0) AS successes,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       COALESCE(AVG(duration_ms), 0) AS average_duration_ms
                FROM requests
                """
            ).fetchone()
        payload = dict(request_row)
        payload.update(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "database": str(self.path),
                "affinities": affinity_count,
                "checkpoints": checkpoint_count,
            }
        )
        return payload

    def checkpoints(self, *, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        with _DB_LOCK, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM checkpoints ORDER BY updated_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_requests(
        self,
        *,
        session_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM requests"
        params: list[Any] = []
        if session_id:
            query += " WHERE session_id = ?"
            params.append(session_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with _DB_LOCK, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def clear(self, *, session_id: str = "", checkpoints: bool = False) -> None:
        with _DB_LOCK, self._connect() as connection:
            if session_id:
                connection.execute(
                    "DELETE FROM session_affinity WHERE session_id = ?",
                    (session_id,),
                )
                connection.execute(
                    "DELETE FROM requests WHERE session_id = ?",
                    (session_id,),
                )
            else:
                connection.execute("DELETE FROM session_affinity")
                connection.execute("DELETE FROM requests")
            if checkpoints:
                connection.execute("DELETE FROM checkpoints")


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CacheRouteCandidate",
    "CacheStateStore",
    "PromptCacheManifest",
    "build_cache_headers",
    "build_manifest",
    "classify_engine",
    "hash_json",
    "hash_text",
    "is_cache_eligible_endpoint",
    "route_score",
    "select_best_route",
]
