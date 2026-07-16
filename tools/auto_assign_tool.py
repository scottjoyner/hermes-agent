"""Auto-Assign integration tools for claiming, heartbeating, and completing work assignments."""

import json
import logging
import os
import platform
import uuid
from typing import Any

import httpx

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

AUTO_ASSIGN_ENV = "AUTO_ASSIGN_BASE_URL"


def _base_url() -> str | None:
    return os.getenv(AUTO_ASSIGN_ENV)


def check_auto_assign_requirements() -> bool:
    return bool(_base_url())


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

AUTO_ASSIGN_STATUS_SCHEMA = {
    "name": "auto_assign_status",
    "description": (
        "Check the auto-assign service for pending assignments available to claim. "
        "Returns the list of recommended or unclaimed assignments for this worker. "
        "Call this first to see what work is available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of pending assignments to return",
                "default": 10,
            },
        },
    },
}

AUTO_ASSIGN_CLAIM_SCHEMA = {
    "name": "auto_assign_claim",
    "description": (
        "Claim an assignment from auto-assign. This acquires a lease on the task "
        "so no other worker can claim it. Returns the assignment details including "
        "lease expiry. You must heartbeat periodically to keep the lease alive."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignment_id": {
                "type": "string",
                "description": "The assignment ID to claim (from auto_assign_status)",
            },
            "task_id": {
                "type": "string",
                "description": "The task ID associated with this assignment",
            },
            "lease_seconds": {
                "type": "integer",
                "description": "Lease duration in seconds (default 900 = 15 min)",
                "default": 900,
            },
            "capabilities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Worker capabilities to advertise",
            },
        },
        "required": ["assignment_id", "task_id"],
    },
}

AUTO_ASSIGN_HEARTBEAT_SCHEMA = {
    "name": "auto_assign_heartbeat",
    "description": (
        "Send a heartbeat to auto-assign to renew the lease on a claimed assignment. "
        "Must be called periodically (at least every 5 minutes) to prevent the "
        "assignment from expiring and being reassigned to another worker."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignment_id": {
                "type": "string",
                "description": "The claimed assignment ID",
            },
            "status": {
                "type": "string",
                "description": "Current work status: 'running', 'paused', 'nearly_done'",
                "default": "running",
            },
            "progress_note": {
                "type": "string",
                "description": "Optional short note about current progress",
            },
        },
        "required": ["assignment_id"],
    },
}

AUTO_ASSIGN_COMPLETE_SCHEMA = {
    "name": "auto_assign_complete",
    "description": (
        "Mark an assignment as complete or failed in auto-assign. This releases "
        "the lease and records the outcome. Must be called when work is finished."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignment_id": {
                "type": "string",
                "description": "The assignment ID to complete",
            },
            "task_id": {
                "type": "string",
                "description": "The task ID",
            },
            "status": {
                "type": "string",
                "enum": ["success", "failure"],
                "description": "Outcome status",
                "default": "success",
            },
            "summary": {
                "type": "string",
                "description": "Summary of what was done or why it failed",
            },
            "artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
                "description": "Optional list of artifact paths produced",
            },
        },
        "required": ["assignment_id", "task_id"],
    },
}

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _worker_id() -> str:
    return os.getenv("HERMES_WORKER_ID", "hermes-agent")


def _node_id() -> str:
    return platform.node() or os.uname().nodename


def _handle_auto_assign_status(args: dict, **kw) -> str:
    base = _base_url()
    if not base:
        return tool_error("AUTO_ASSIGN_BASE_URL not set")
    try:
        with httpx.Client(base_url=base, timeout=30.0) as c:
            resp = c.get("/api/assignments", params={"limit": args.get("limit", 10)})
            resp.raise_for_status()
            data = resp.json()
            return tool_result({"assignments": data, "count": len(data)})
    except Exception as e:
        return tool_error(f"auto-assign status check failed: {e}")


def _handle_auto_assign_claim(args: dict, **kw) -> str:
    base = _base_url()
    if not base:
        return tool_error("AUTO_ASSIGN_BASE_URL not set")
    try:
        assignment_id = args["assignment_id"]
        correlation_id = kw.get("correlation_id") or str(uuid.uuid4())
        body = {
            "correlation_id": correlation_id,
            "task_id": args["task_id"],
            "worker_id": _worker_id(),
            "node_id": _node_id(),
            "capabilities": args.get("capabilities", ["terminal", "web_search", "file"]),
            "lease_seconds": args.get("lease_seconds", 900),
            "links": {
                "correlation_id": correlation_id,
                "dispatch_id": kw.get("dispatch_id"),
                "task_id": args["task_id"],
                "route_id": kw.get("route_id"),
                "assignment_id": assignment_id,
            },
        }
        with httpx.Client(base_url=base, timeout=30.0) as c:
            resp = c.post(f"/api/assignments/{assignment_id}/claim", json=body)
            resp.raise_for_status()
            return tool_result(resp.json())
    except httpx.HTTPStatusError as e:
        detail = "unknown"
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text[:500]
        return tool_error(f"claim failed ({e.response.status_code}): {detail}")
    except Exception as e:
        return tool_error(f"claim failed: {e}")


def _handle_auto_assign_heartbeat(args: dict, **kw) -> str:
    base = _base_url()
    if not base:
        return tool_error("AUTO_ASSIGN_BASE_URL not set")
    try:
        assignment_id = args["assignment_id"]
        correlation_id = kw.get("correlation_id") or str(uuid.uuid4())
        body = {
            "node_id": _node_id(),
            "worker_id": _worker_id(),
            "assignment_id": assignment_id,
            "status": args.get("status", "running"),
            "correlation_id": correlation_id,
        }
        with httpx.Client(base_url=base, timeout=30.0) as c:
            resp = c.post("/api/heartbeats", json=body)
            resp.raise_for_status()
            return tool_result(resp.json())
    except httpx.HTTPStatusError as e:
        detail = "unknown"
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text[:500]
        return tool_error(f"heartbeat failed ({e.response.status_code}): {detail}")
    except Exception as e:
        return tool_error(f"heartbeat failed: {e}")


def _handle_auto_assign_complete(args: dict, **kw) -> str:
    base = _base_url()
    if not base:
        return tool_error("AUTO_ASSIGN_BASE_URL not set")
    try:
        assignment_id = args["assignment_id"]
        correlation_id = kw.get("correlation_id") or str(uuid.uuid4())
        body = {
            "correlation_id": correlation_id,
            "assignment_id": assignment_id,
            "task_id": args["task_id"],
            "worker_id": _worker_id(),
            "status": args.get("status", "success"),
            "summary": args.get("summary", ""),
            "artifacts": args.get("artifacts", []),
            "links": {
                "correlation_id": correlation_id,
                "dispatch_id": kw.get("dispatch_id"),
                "task_id": args["task_id"],
                "route_id": kw.get("route_id"),
                "assignment_id": assignment_id,
            },
        }
        with httpx.Client(base_url=base, timeout=30.0) as c:
            resp = c.post(f"/api/assignments/{assignment_id}/complete", json=body)
            resp.raise_for_status()
            return tool_result(resp.json())
    except httpx.HTTPStatusError as e:
        detail = "unknown"
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text[:500]
        return tool_error(f"complete failed ({e.response.status_code}): {detail}")
    except Exception as e:
        return tool_error(f"complete failed: {e}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry.register(
    name="auto_assign_status",
    toolset="auto_assign",
    schema=AUTO_ASSIGN_STATUS_SCHEMA,
    handler=_handle_auto_assign_status,
    check_fn=check_auto_assign_requirements,
    requires_env=[AUTO_ASSIGN_ENV],
    emoji="📋",
)

registry.register(
    name="auto_assign_claim",
    toolset="auto_assign",
    schema=AUTO_ASSIGN_CLAIM_SCHEMA,
    handler=_handle_auto_assign_claim,
    check_fn=check_auto_assign_requirements,
    requires_env=[AUTO_ASSIGN_ENV],
    emoji="🔒",
)

registry.register(
    name="auto_assign_heartbeat",
    toolset="auto_assign",
    schema=AUTO_ASSIGN_HEARTBEAT_SCHEMA,
    handler=_handle_auto_assign_heartbeat,
    check_fn=check_auto_assign_requirements,
    requires_env=[AUTO_ASSIGN_ENV],
    emoji="💓",
)

registry.register(
    name="auto_assign_complete",
    toolset="auto_assign",
    schema=AUTO_ASSIGN_COMPLETE_SCHEMA,
    handler=_handle_auto_assign_complete,
    check_fn=check_auto_assign_requirements,
    requires_env=[AUTO_ASSIGN_ENV],
    emoji="✅",
)
