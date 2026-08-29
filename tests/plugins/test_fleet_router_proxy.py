from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "fleet-router"


def _load_module():
    name = "test_fleet_router_proxy_impl"
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


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") not in {
            "/models",
            "/v1/models",
        }:
            self.send_error(404)
            return
        payload = {
            "object": "list",
            "data": [
                {
                    "id": self.server.model_id,
                    "object": "model",
                }
            ],
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(
            {
                "path": self.path,
                "payload": payload,
                "authorization": self.headers.get("Authorization"),
                "checkpoint": self.headers.get(
                    "X-Hermes-Cache-Checkpoint-Id"
                ),
            }
        )
        if self.server.fail_posts:
            body = json.dumps(
                {"error": {"message": "temporary failure"}}
            ).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload.get("stream"):
            body = (
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _UpstreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, model_id: str, fail_posts: bool = False):
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)
        self.model_id = model_id
        self.fail_posts = fail_posts
        self.requests = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


def _start(server):
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    return thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _request_json(url: str, payload: dict, headers: dict | None = None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return (
            response.status,
            dict(response.headers),
            response.read(),
        )


def test_proxy_routes_alias_strips_inbound_auth_and_passes_cache_header():
    module = _load_module()
    upstream = _UpstreamServer("real-model")
    upstream_thread = _start(upstream)
    proxy = None
    proxy_thread = None
    try:
        config = module.core.parse_fleet_config(
            {
                "fleet": {
                    "enabled": True,
                    "health_ttl_seconds": 60,
                    "nodes": [
                        {
                            "name": "node-a",
                            "base_url": upstream.base_url,
                            "context_length": 32768,
                            "model_map": {
                                "friendly-model": "real-model"
                            },
                        }
                    ],
                }
            }
        )
        router = module.FleetRouter(config)
        router.refresh(force=True)
        proxy = module.proxy.FleetProxyServer(
            ("127.0.0.1", 0),
            router,
        )
        proxy_thread = _start(proxy)

        status, headers, body = _request_json(
            (
                f"http://127.0.0.1:{proxy.server_port}"
                "/v1/chat/completions"
            ),
            {
                "model": "friendly-model",
                "messages": [
                    {"role": "user", "content": "hello"}
                ],
            },
            {
                "Authorization": "Bearer must-not-forward",
                "X-Hermes-Cache-Checkpoint-Id": "checkpoint-a",
            },
        )

        decoded = json.loads(body.decode("utf-8"))
        assert status == 200
        assert headers["X-Hermes-Fleet-Node"] == "node-a"
        assert decoded["model"] == "real-model"
        assert len(upstream.requests) == 1
        observed = upstream.requests[0]
        assert observed["payload"]["model"] == "real-model"
        assert observed["authorization"] is None
        assert observed["checkpoint"] == "checkpoint-a"
    finally:
        if proxy is not None and proxy_thread is not None:
            _stop(proxy, proxy_thread)
        _stop(upstream, upstream_thread)


def test_proxy_retries_a_second_node_after_upstream_5xx():
    module = _load_module()
    failing = _UpstreamServer("shared-model", fail_posts=True)
    healthy = _UpstreamServer("shared-model")
    failing_thread = _start(failing)
    healthy_thread = _start(healthy)
    proxy = None
    proxy_thread = None
    try:
        config = module.core.parse_fleet_config(
            {
                "fleet": {
                    "enabled": True,
                    "max_attempts": 2,
                    "health_ttl_seconds": 60,
                    "nodes": [
                        {
                            "name": "a-failing",
                            "base_url": failing.base_url,
                            "models": ["shared-model"],
                        },
                        {
                            "name": "b-healthy",
                            "base_url": healthy.base_url,
                            "models": ["shared-model"],
                        },
                    ],
                }
            }
        )
        router = module.FleetRouter(config)
        router.refresh(force=True)
        for state in router._nodes.values():
            state.latency_ema_ms = 10.0
        proxy = module.proxy.FleetProxyServer(
            ("127.0.0.1", 0),
            router,
        )
        proxy_thread = _start(proxy)

        status, headers, body = _request_json(
            (
                f"http://127.0.0.1:{proxy.server_port}"
                "/v1/chat/completions"
            ),
            {
                "model": "shared-model",
                "messages": [
                    {"role": "user", "content": "hello"}
                ],
            },
        )

        assert status == 200
        assert headers["X-Hermes-Fleet-Node"] == "b-healthy"
        assert json.loads(body.decode("utf-8"))["model"] == "shared-model"
        assert len(failing.requests) == 1
        assert len(healthy.requests) == 1
    finally:
        if proxy is not None and proxy_thread is not None:
            _stop(proxy, proxy_thread)
        _stop(failing, failing_thread)
        _stop(healthy, healthy_thread)


def test_proxy_relays_streaming_chat_completion_bytes():
    module = _load_module()
    upstream = _UpstreamServer("stream-model")
    upstream_thread = _start(upstream)
    proxy = None
    proxy_thread = None
    try:
        config = module.core.parse_fleet_config(
            {
                "fleet": {
                    "enabled": True,
                    "health_ttl_seconds": 60,
                    "nodes": [
                        {
                            "name": "stream-node",
                            "base_url": upstream.base_url,
                            "models": ["stream-model"],
                        }
                    ],
                }
            }
        )
        router = module.FleetRouter(config)
        router.refresh(force=True)
        proxy = module.proxy.FleetProxyServer(
            ("127.0.0.1", 0),
            router,
        )
        proxy_thread = _start(proxy)

        status, headers, body = _request_json(
            (
                f"http://127.0.0.1:{proxy.server_port}"
                "/v1/chat/completions"
            ),
            {
                "model": "stream-model",
                "messages": [
                    {"role": "user", "content": "hello"}
                ],
                "stream": True,
            },
        )

        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert headers["X-Hermes-Fleet-Node"] == "stream-node"
        assert b'"content":"ok"' in body
        assert body.endswith(b"data: [DONE]\n\n")
    finally:
        if proxy is not None and proxy_thread is not None:
            _stop(proxy, proxy_thread)
        _stop(upstream, upstream_thread)
