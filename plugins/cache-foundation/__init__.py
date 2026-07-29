"""Cache-aware inference foundation for Hermes.

The plugin fingerprints exact provider requests, keeps session-to-endpoint affinity,
adds cache-routing headers to trusted local inference endpoints, and records cache
telemetry around the real provider execution.  It is intentionally engine-neutral:
a llama.cpp controller, LM Studio, or a fleet proxy may consume the headers without
requiring changes to Hermes' planner or provider transports.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.cache_foundation import (
    CACHE_SCHEMA_VERSION,
    CacheStateStore,
    PromptCacheManifest,
    build_cache_headers,
    build_manifest,
    classify_engine,
    is_cache_eligible_endpoint,
)

_STORE: Optional[CacheStateStore] = None
_STORE_LOCK = threading.RLock()
_PENDING: dict[str, PromptCacheManifest] = {}
_PENDING_LOCK = threading.RLock()
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def _disabled() -> bool:
    return _truthy(os.getenv("HERMES_CACHE_DISABLE"))


def _store() -> CacheStateStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            configured = os.getenv("HERMES_CACHE_STATE_DB", "").strip()
            _STORE = CacheStateStore(configured or None)
        return _STORE


def _reset_store_for_tests() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
    with _PENDING_LOCK:
        _PENDING.clear()


def _build_manifest_from_kwargs(kwargs: Mapping[str, Any]) -> PromptCacheManifest:
    request = kwargs.get("request")
    if not isinstance(request, Mapping):
        request = {}
    return build_manifest(
        request,
        session_id=str(kwargs.get("session_id") or ""),
        model=str(kwargs.get("model") or ""),
        provider=str(kwargs.get("provider") or ""),
        api_mode=str(kwargs.get("api_mode") or ""),
        base_url=str(kwargs.get("base_url") or ""),
    )


def _cache_request(**kwargs: Any) -> dict[str, Any] | None:
    """Attach cache identifiers without changing provider request semantics."""

    if _disabled():
        return None
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None

    api_mode = str(kwargs.get("api_mode") or "")
    if api_mode != "chat_completions":
        return None

    base_url = str(kwargs.get("base_url") or "")
    provider = str(kwargs.get("provider") or "")
    if not is_cache_eligible_endpoint(base_url, provider):
        return None

    manifest = _build_manifest_from_kwargs(kwargs)
    updated = dict(request)
    headers = dict(updated.get("extra_headers") or {})
    for key, value in build_cache_headers(manifest).items():
        headers.setdefault(key, value)
    updated["extra_headers"] = headers

    try:
        state = _store()
        state.bind_session(manifest)
        state.register_checkpoint(manifest, state="observed")
    except Exception as exc:
        print(f"cache-foundation: state write failed: {exc}", file=sys.stderr)

    request_id = str(kwargs.get("api_request_id") or "")
    if request_id:
        with _PENDING_LOCK:
            _PENDING[request_id] = manifest

    return {
        "request": updated,
        "source": "cache-foundation",
        "reason": "attached local cache manifest and session affinity",
    }


def _read_attr(value: Any, name: str, default: Any = 0) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _usage_metrics(response: Any) -> dict[str, int]:
    usage = _read_attr(response, "usage", None)
    if usage is None:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    prompt_tokens = int(_read_attr(usage, "prompt_tokens", 0) or _read_attr(usage, "input_tokens", 0) or 0)
    completion_tokens = int(
        _read_attr(usage, "completion_tokens", 0)
        or _read_attr(usage, "output_tokens", 0)
        or 0
    )
    cache_read = int(
        _read_attr(usage, "cache_read_input_tokens", 0)
        or _read_attr(usage, "cached_tokens", 0)
        or 0
    )
    cache_write = int(
        _read_attr(usage, "cache_creation_input_tokens", 0)
        or _read_attr(usage, "cache_write_tokens", 0)
        or 0
    )

    details = _read_attr(usage, "prompt_tokens_details", None)
    if details is not None:
        cache_read = max(cache_read, int(_read_attr(details, "cached_tokens", 0) or 0))

    input_details = _read_attr(usage, "input_tokens_details", None)
    if input_details is not None:
        cache_read = max(cache_read, int(_read_attr(input_details, "cached_tokens", 0) or 0))

    return {
        "prompt_tokens": max(0, prompt_tokens),
        "completion_tokens": max(0, completion_tokens),
        "cache_read_tokens": max(0, cache_read),
        "cache_write_tokens": max(0, cache_write),
    }


def _cache_execution(**kwargs: Any) -> Any:
    """Measure the actual provider call while preserving middleware semantics."""

    next_call = kwargs["next_call"]
    request = kwargs.get("request")
    if _disabled() or not isinstance(request, dict):
        return next_call(request)

    base_url = str(kwargs.get("base_url") or "")
    provider = str(kwargs.get("provider") or "")
    api_mode = str(kwargs.get("api_mode") or "")
    if api_mode != "chat_completions" or not is_cache_eligible_endpoint(base_url, provider):
        return next_call(request)

    request_id = str(kwargs.get("api_request_id") or "")
    with _PENDING_LOCK:
        manifest = _PENDING.pop(request_id, None)
    if manifest is None:
        manifest = _build_manifest_from_kwargs(kwargs)

    started = time.monotonic()
    try:
        response = next_call(request)
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            _store().record_request(
                api_request_id=request_id,
                manifest=manifest,
                duration_ms=duration_ms,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception as store_exc:
            print(f"cache-foundation: telemetry write failed: {store_exc}", file=sys.stderr)
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    try:
        metrics = _usage_metrics(response)
        state = _store()
        state.record_request(
            api_request_id=request_id,
            manifest=manifest,
            duration_ms=duration_ms,
            success=True,
            **metrics,
        )
        if metrics["cache_read_tokens"] > 0:
            state.register_checkpoint(
                manifest,
                state="hit",
                prefix_tokens=metrics["cache_read_tokens"],
            )
        elif metrics["cache_write_tokens"] > 0:
            state.register_checkpoint(
                manifest,
                state="written",
                prefix_tokens=metrics["cache_write_tokens"],
            )
    except Exception as exc:
        print(f"cache-foundation: telemetry write failed: {exc}", file=sys.stderr)
    return response


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="cache_command")

    status = commands.add_parser("status", help="Show cache-affinity and telemetry status")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    inspect = commands.add_parser("inspect", help="Inspect recent cache requests")
    inspect.add_argument("--session", default="", help="Filter by Hermes session ID")
    inspect.add_argument("--limit", type=int, default=20)

    checkpoints = commands.add_parser("checkpoints", help="List known checkpoint inventory")
    checkpoints.add_argument("--limit", type=int, default=20)

    clear = commands.add_parser("clear", help="Clear affinity and telemetry state")
    clear.add_argument("--session", default="", help="Clear only one session")
    clear.add_argument(
        "--checkpoints",
        action="store_true",
        help="Also clear checkpoint inventory",
    )

    doctor = commands.add_parser("doctor", help="Inspect cache configuration and endpoint type")
    doctor.add_argument("--endpoint", default="", help="Optional endpoint to probe")
    doctor.add_argument("--provider", default="custom")
    doctor.add_argument("--timeout", type=float, default=3.0)

    warm = commands.add_parser("warm", help="Warm an OpenAI-compatible local prefix")
    warm.add_argument("--endpoint", required=True)
    warm.add_argument("--model", required=True)
    warm.add_argument("--prompt-file", required=True)
    warm.add_argument("--timeout", type=float, default=120.0)

    parser.set_defaults(func=_handle_cli)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _cmd_status(as_json: bool) -> int:
    summary = _store().summary()
    summary.update(
        {
            "enabled": not _disabled(),
            "remote_headers_allowed": _truthy(os.getenv("HERMES_CACHE_ALLOW_REMOTE")),
            "mode": os.getenv("HERMES_CACHE_MODE", "prefer"),
            "engine_override": os.getenv("HERMES_CACHE_ENGINE_ID", ""),
            "model_fingerprint_override": os.getenv(
                "HERMES_CACHE_MODEL_FINGERPRINT", ""
            ),
            "chat_template_hash": os.getenv("HERMES_CACHE_CHAT_TEMPLATE_HASH", ""),
            "kv_format": os.getenv("HERMES_CACHE_KV_FORMAT", "managed"),
        }
    )
    if as_json:
        _print_json(summary)
        return 0
    print("Hermes cache foundation")
    print("-----------------------")
    for key, value in summary.items():
        print(f"{key:24}: {value}")
    return 0


def _cmd_inspect(session_id: str, limit: int) -> int:
    payload = {
        "session": _store().affinity(session_id) if session_id else None,
        "requests": _store().recent_requests(session_id=session_id, limit=limit),
    }
    _print_json(payload)
    return 0


def _cmd_checkpoints(limit: int) -> int:
    _print_json(_store().checkpoints(limit=limit))
    return 0


def _cmd_clear(session_id: str, checkpoints: bool) -> int:
    _store().clear(session_id=session_id, checkpoints=checkpoints)
    scope = f"session {session_id}" if session_id else "all sessions"
    print(f"Cleared cache state for {scope}.")
    if checkpoints:
        print("Checkpoint inventory cleared.")
    return 0


def _probe_json(url: str, timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout)) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return int(response.status), json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except Exception:
        return 0, None


def _cmd_doctor(endpoint: str, provider: str, timeout: float) -> int:
    payload: dict[str, Any] = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "enabled": not _disabled(),
        "endpoint": endpoint,
        "eligible": is_cache_eligible_endpoint(endpoint, provider) if endpoint else None,
        "provider": provider,
        "engine": classify_engine(provider, endpoint),
        "control_mode": (
            "explicit"
            if classify_engine(provider, endpoint) == "llama.cpp"
            else "managed"
        ),
    }
    if endpoint:
        base = endpoint.rstrip("/")
        probes = {}
        for path in ("/v1/models", "/api/v1/models", "/props"):
            status, body = _probe_json(base + path, timeout)
            probes[path] = {
                "status": status,
                "reachable": bool(status),
                "shape": type(body).__name__ if body is not None else None,
            }
        payload["probes"] = probes
    _print_json(payload)
    return 0 if payload["enabled"] and (not endpoint or payload["eligible"]) else 1


def _warm_url(endpoint: str) -> str:
    value = endpoint.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def _cmd_warm(endpoint: str, model: str, prompt_file: str, timeout: float) -> int:
    path = Path(prompt_file).expanduser()
    if not path.is_file():
        print(f"Prompt file not found: {path}", file=sys.stderr)
        return 2
    if not is_cache_eligible_endpoint(endpoint, "custom"):
        print(
            "Refusing to send cache warmup to a remote endpoint. "
            "Set HERMES_CACHE_ALLOW_REMOTE=1 only for a trusted cache proxy.",
            file=sys.stderr,
        )
        return 2

    prompt = path.read_text(encoding="utf-8")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": "Reply with OK."}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    manifest = build_manifest(
        body,
        model=model,
        provider="custom",
        api_mode="chat_completions",
        base_url=endpoint,
    )
    headers = {"Content-Type": "application/json", **build_cache_headers(manifest)}
    api_key = os.getenv("HERMES_CACHE_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        _warm_url(endpoint),
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout)) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
    except Exception as exc:
        print(f"Cache warmup failed: {exc}", file=sys.stderr)
        return 1

    duration_ms = int((time.monotonic() - started) * 1000)
    metrics = _usage_metrics(payload)
    state = _store()
    state.register_checkpoint(
        manifest,
        state="warmed",
        prefix_tokens=metrics["prompt_tokens"],
    )
    state.record_request(
        api_request_id="",
        manifest=manifest,
        duration_ms=duration_ms,
        success=True,
        **metrics,
    )
    _print_json(
        {
            "checkpoint_id": manifest.checkpoint_id,
            "endpoint": manifest.endpoint,
            "duration_ms": duration_ms,
            **metrics,
        }
    )
    return 0


def _handle_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "cache_command", None)
    if command == "status":
        return _cmd_status(bool(getattr(args, "json", False)))
    if command == "inspect":
        return _cmd_inspect(
            str(getattr(args, "session", "")),
            int(getattr(args, "limit", 20)),
        )
    if command == "checkpoints":
        return _cmd_checkpoints(int(getattr(args, "limit", 20)))
    if command == "clear":
        return _cmd_clear(
            str(getattr(args, "session", "")),
            bool(getattr(args, "checkpoints", False)),
        )
    if command == "doctor":
        return _cmd_doctor(
            str(getattr(args, "endpoint", "")),
            str(getattr(args, "provider", "custom")),
            float(getattr(args, "timeout", 3.0)),
        )
    if command == "warm":
        return _cmd_warm(
            str(getattr(args, "endpoint", "")),
            str(getattr(args, "model", "")),
            str(getattr(args, "prompt_file", "")),
            float(getattr(args, "timeout", 120.0)),
        )
    print("usage: hermes cache {status,inspect,checkpoints,clear,doctor,warm}")
    return 2


def register(ctx: Any) -> None:
    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(
            "cache",
            "Inspect, warm, and manage cache-aware inference state",
            _setup_cli,
            _handle_cli,
            description="KV/prefix cache manifests, affinity, and telemetry",
        )

    if _disabled():
        return
    ctx.register_middleware("llm_request", _cache_request)
    ctx.register_middleware("llm_execution", _cache_execution)


__all__ = [
    "register",
    "_cache_execution",
    "_cache_request",
    "_reset_store_for_tests",
    "_usage_metrics",
]
