"""Shared auto-assign client used by both the agent tool and the CLI worker.

LLD W-81: ``tools/auto_assign_tool.py`` (agent tool) and
``hermes_cli/auto_assign_worker.py`` (CLI worker) previously re-defined
``AUTO_ASSIGN_ENV``, ``_worker_id()``, ``_node_id()``, and the httpx call
patterns independently. This module is the single source of truth for the
common client + correlation/link helpers so both sides stay in lock-step.

It intentionally does NOT import ``tools.registry`` or the CLI config so it
stays importable from both contexts without pulling in heavy deps.
"""

from __future__ import annotations

import os
import platform
import uuid
from typing import Any, Optional

import httpx

# Shared env var that points at the auto-assign service base URL.
AUTO_ASSIGN_ENV = "AUTO_ASSIGN_BASE_URL"

# Default lease duration (seconds) advertised on claim — 900 = 15 min.
DEFAULT_LEASE_SECONDS = 900

HTTP_TIMEOUT = 30.0


def _base_url() -> Optional[str]:
    return os.getenv(AUTO_ASSIGN_ENV)


def check_requirements() -> bool:
    return bool(_base_url())


def _worker_id() -> str:
    return os.getenv("HERMES_WORKER_ID", "hermes-agent")


def _node_id() -> str:
    return platform.node() or os.uname().nodename


def new_correlation_id() -> str:
    """Generate a fresh correlation_id (hex UUID string)."""
    return uuid.uuid4().hex


def build_links(correlation_id: str, assignment_id: str,
                task_id: Optional[str] = None,
                dispatch_id: Optional[str] = None,
                route_id: Optional[str] = None) -> dict[str, Any]:
    """Build the ``links`` payload required by the auto-assign API.

    Mirrors the contract-envelope ``links[]`` shape locally (correlation_id
    required) per the HLD/LLD event-envelope guidance until the shared
    ``swarm-contracts`` package is wired in.
    """
    return {
        "correlation_id": correlation_id,
        "dispatch_id": dispatch_id,
        "task_id": task_id,
        "route_id": route_id,
        "assignment_id": assignment_id,
    }


def fetch_assignments(base: str, limit: int = 10) -> list[dict[str, Any]]:
    with httpx.Client(base_url=base, timeout=HTTP_TIMEOUT) as c:
        resp = c.get("/api/assignments", params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else data.get("assignments", [])


def claim_assignment(base: str, assignment_id: str, task_id: str,
                     correlation_id: Optional[str] = None,
                     capabilities: Optional[list[str]] = None,
                     lease_seconds: int = DEFAULT_LEASE_SECONDS,
                     dispatch_id: Optional[str] = None,
                     route_id: Optional[str] = None) -> dict[str, Any]:
    correlation_id = correlation_id or new_correlation_id()
    body = {
        "correlation_id": correlation_id,
        "task_id": task_id,
        "worker_id": _worker_id(),
        "node_id": _node_id(),
        "capabilities": capabilities if capabilities is not None else ["terminal", "web_search", "file"],
        "lease_seconds": lease_seconds,
        "links": build_links(correlation_id, assignment_id, task_id=task_id,
                             dispatch_id=dispatch_id, route_id=route_id),
    }
    with httpx.Client(base_url=base, timeout=HTTP_TIMEOUT) as c:
        resp = c.post(f"/api/assignments/{assignment_id}/claim", json=body)
        resp.raise_for_status()
        return resp.json()


def send_heartbeat(base: str, assignment_id: str, status: str = "running",
                   correlation_id: Optional[str] = None) -> dict[str, Any]:
    correlation_id = correlation_id or new_correlation_id()
    body = {
        "node_id": _node_id(),
        "worker_id": _worker_id(),
        "assignment_id": assignment_id,
        "status": status,
        "correlation_id": correlation_id,
    }
    with httpx.Client(base_url=base, timeout=HTTP_TIMEOUT) as c:
        resp = c.post("/api/heartbeats", json=body)
        resp.raise_for_status()
        return resp.json()


def complete_assignment(base: str, assignment_id: str, task_id: str,
                        correlation_id: Optional[str] = None,
                        status: str = "success", summary: str = "",
                        artifacts: Optional[list[dict[str, Any]]] = None,
                        dispatch_id: Optional[str] = None,
                        route_id: Optional[str] = None) -> dict[str, Any]:
    correlation_id = correlation_id or new_correlation_id()
    body = {
        "correlation_id": correlation_id,
        "assignment_id": assignment_id,
        "task_id": task_id,
        "worker_id": _worker_id(),
        "status": status,
        "summary": summary,
        "artifacts": artifacts if artifacts is not None else [],
        "links": build_links(correlation_id, assignment_id, task_id=task_id,
                             dispatch_id=dispatch_id, route_id=route_id),
    }
    with httpx.Client(base_url=base, timeout=HTTP_TIMEOUT) as c:
        resp = c.post(f"/api/assignments/{assignment_id}/complete", json=body)
        resp.raise_for_status()
        return resp.json()
