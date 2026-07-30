"""Threaded OpenAI-compatible HTTP proxy for the Hermes fleet router."""

from __future__ import annotations

import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from hermes_cli.urllib_security import open_credentialed_url

from .core import FleetRouter, integer, join_endpoint, requirements_from_payload


class FleetProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HermesFleetRouter/0.1"

    @property
    def router(self) -> FleetRouter:
        return self.server.router  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        logger = getattr(self.server, "logger", None)
        if logger is not None:
            logger.info("fleet-proxy: " + format, *args)

    def _json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        env_name = self.router.config.listen_token_env
        if not env_name:
            return True
        expected = os.getenv(env_name, "").strip()
        supplied = str(self.headers.get("Authorization") or "")
        return bool(expected and supplied == f"Bearer {expected}")

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"error": {"message": "unauthorized"}})
            return
        path = self.path.rstrip("/")
        if path in {"", "/health", "/fleet/status"}:
            if path == "/health":
                self.router.refresh()
            self._json(200, self.router.status())
            return
        if path in {"/models", "/v1/models"}:
            self.router.refresh()
            self._json(200, {
                "object": "list",
                "data": [
                    {"id": model, "object": "model", "owned_by": "hermes-fleet"}
                    for model in self.router.all_models()
                ],
            })
            return
        self._json(404, {"error": {"message": "not found"}})

    def _read_payload(self) -> dict[str, Any] | None:
        length = integer(self.headers.get("Content-Length"))
        if length <= 0:
            self._json(411, {"error": {"message": "Content-Length required"}})
            return None
        if length > self.router.config.max_request_bytes:
            self._json(413, {"error": {"message": "request too large"}})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self._json(400, {"error": {"message": "invalid JSON body"}})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": {"message": "JSON object required"}})
            return None
        return payload

    def _relay(self, response: Any, node_name: str, stream: bool) -> None:
        self.send_response(int(getattr(response, "status", 200) or 200))
        self.send_header(
            "Content-Type",
            response.headers.get("Content-Type", "application/json"),
        )
        self.send_header("X-Hermes-Fleet-Node", node_name)
        request_id = response.headers.get("X-Request-Id")
        if request_id:
            self.send_header("X-Request-Id", request_id)
        if stream:
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            return
        body = response.read()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"error": {"message": "unauthorized"}})
            return
        if self.path.rstrip("/") not in {
            "/chat/completions",
            "/v1/chat/completions",
        }:
            self._json(404, {"error": {"message": "unsupported endpoint"}})
            return
        payload = self._read_payload()
        if payload is None:
            return
        if not str(payload.get("model") or "").strip():
            self._json(400, {"error": {"message": "model is required"}})
            return

        incoming = {str(key): str(value) for key, value in self.headers.items()}
        requirements = requirements_from_payload(
            payload,
            incoming,
            self.router.config.default_max_output,
        )
        self.router.refresh()
        attempted: list[str] = []
        last_error = "no eligible fleet node"

        for _ in range(self.router.config.max_attempts):
            decision = self.router.route(requirements, attempted)
            if decision is None:
                break
            attempted.append(decision.node_name)
            node = self.router.acquire(decision)
            routed = dict(payload)
            routed["model"] = decision.upstream_model
            request = urllib.request.Request(
                join_endpoint(node.base_url, self.path),
                data=json.dumps(routed, ensure_ascii=False).encode("utf-8"),
                headers=self.router.upstream_headers(node, incoming),
                method="POST",
            )
            started = time.monotonic()
            try:
                response = open_credentialed_url(
                    request,
                    timeout=node.request_timeout,
                )
                duration = (time.monotonic() - started) * 1000
                self.router.release(
                    decision,
                    True,
                    duration,
                    requirements.session_id,
                    requirements.checkpoint_id,
                )
                self._relay(
                    response,
                    decision.node_name,
                    bool(payload.get("stream")),
                )
                return
            except urllib.error.HTTPError as exc:
                duration = (time.monotonic() - started) * 1000
                last_error = f"HTTP {exc.code}: {exc.reason}"
                self.router.release(decision, False, duration, error=last_error)
                if exc.code < 500:
                    body = exc.read()
                    self.send_response(exc.code)
                    self.send_header(
                        "Content-Type",
                        exc.headers.get("Content-Type", "application/json"),
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header(
                        "X-Hermes-Fleet-Node",
                        decision.node_name,
                    )
                    self.end_headers()
                    self.wfile.write(body)
                    return
            except Exception as exc:
                duration = (time.monotonic() - started) * 1000
                last_error = f"{type(exc).__name__}: {exc}"
                self.router.release(decision, False, duration, error=last_error)

        self._json(503, {
            "error": {
                "message": last_error,
                "attempted_nodes": attempted,
            }
        })


class FleetProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], router: FleetRouter):
        super().__init__(address, FleetProxyHandler)
        self.router = router
        self.logger = None


def is_loopback_bind(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def serve(
    router: FleetRouter,
    host: str | None = None,
    port: int | None = None,
) -> None:
    bind_host = str(host or router.config.listen_host).strip()
    bind_port = int(port or router.config.listen_port)
    if not is_loopback_bind(bind_host) and not router.config.allow_non_loopback:
        raise ValueError(
            "non-loopback fleet binding requires "
            "fleet.listen.allow_non_loopback: true"
        )
    if not router.config.enabled:
        raise ValueError("fleet.enabled must be true before serving")
    if not router.config.nodes:
        raise ValueError(
            "fleet.nodes must contain at least one valid explicit node"
        )
    router.refresh(force=True)
    server = FleetProxyServer((bind_host, bind_port), router)
    try:
        print(
            f"Hermes fleet proxy listening on "
            f"http://{bind_host}:{bind_port}/v1"
        )
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
