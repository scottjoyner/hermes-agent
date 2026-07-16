"""Auto-assign worker — ``hermes auto-assign-worker`` subcommand.

Polls auto-assign for recommended assignments, claims them,
sends heartbeats, and completes them.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime

from hermes_cli.config import load_config
from tools._auto_assign_client import (
    AUTO_ASSIGN_ENV,
    _base_url,
    claim_assignment as _client_claim,
    complete_assignment as _client_complete,
    fetch_assignments,
    send_heartbeat as _client_heartbeat,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("HERMES_AA_POLL_INTERVAL", "30"))
HEARTBEAT_INTERVAL = int(os.getenv("HERMES_AA_HEARTBEAT_INTERVAL", "60"))
LEASE_SECONDS = int(os.getenv("HERMES_AA_LEASE_SECONDS", "900"))


# ---------------------------------------------------------------------------
# Build parser
# ---------------------------------------------------------------------------


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = parent_subparsers.add_parser(
        "auto-assign-worker",
        help="Poll and claim assignments from auto-assign",
    )
    sub = parser.add_subparsers(dest="aaw_command")

    start = sub.add_parser("start", help="Run the worker loop continuously")
    start.add_argument("--interval", type=int, default=POLL_INTERVAL,
                       help="Poll interval in seconds")
    start.add_argument("--once", action="store_true",
                       help="Run a single poll cycle then exit")

    poll_parser = sub.add_parser("poll", help="Run a single poll cycle")
    poll_parser.add_argument("--json", action="store_true",
                             help="Output results as JSON")

    parser.set_defaults(func=auto_assign_worker_command)
    return parser


# ---------------------------------------------------------------------------
# Core worker logic
# ---------------------------------------------------------------------------


def fetch_recommended_assignments(base: str, limit: int = 10) -> list[dict]:
    assignments = fetch_assignments(base, limit=limit)
    return [a for a in assignments if a.get("status") == "recommended"
            and a.get("selected_lane") in ("direct_worker", "local_only")]


# Thin wrappers kept for the CLI worker's own call sites and tests; they
# delegate to the shared client in tools/_auto_assign_client.py (W-81).
def claim_assignment(base: str, assignment: dict) -> dict | None:
    return _client_claim(
        base,
        assignment_id=assignment["assignment_id"],
        task_id=assignment["task_id"],
        lease_seconds=LEASE_SECONDS,
    )


def send_heartbeat(base: str, assignment_id: str, status: str = "running") -> bool:
    _client_heartbeat(base, assignment_id=assignment_id, status=status)
    return True


def complete_assignment(base: str, assignment_id: str, task_id: str,
                        status: str = "success", summary: str = "") -> bool:
    _client_complete(base, assignment_id=assignment_id, task_id=task_id,
                     status=status, summary=summary)
    return True


def execute_work(assignment: dict) -> tuple[str, str]:
    """Execute the assigned work and return (status, summary)."""
    title = assignment.get("title", "unnamed task")
    task_id = assignment.get("task_id", "unknown")
    logger.info("executing assignment %s: %s", assignment.get("assignment_id"), title)
    return ("success", f"executed task {task_id}: {title}")


def poll_cycle(base: str, limit: int = 10) -> list[dict]:
    results = []
    assignments = fetch_recommended_assignments(base, limit=limit)
    logger.info("found %d recommended assignments", len(assignments))

    for assignment in assignments:
        aid = assignment["assignment_id"]
        tid = assignment["task_id"]
        try:
            claim_resp = claim_assignment(base, assignment)
            logger.info("claimed assignment %s: %s", aid, claim_resp)
            hb_count = 0
            last_hb = time.monotonic()

            status, summary = execute_work(assignment)

            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_INTERVAL:
                send_heartbeat(base, aid)
                hb_count += 1
                last_hb = now

            complete_assignment(base, aid, tid, status=status, summary=summary)
            logger.info("completed assignment %s: %s", aid, status)
            results.append({
                "assignment_id": aid,
                "task_id": tid,
                "status": status,
                "summary": summary,
                "heartbeats": hb_count,
            })
        except Exception as e:
            logger.error("failed to process assignment %s: %s", aid, e)
            results.append({
                "assignment_id": aid,
                "task_id": tid,
                "status": "failed",
                "error": str(e),
            })
    return results


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def auto_assign_worker_command(args: argparse.Namespace) -> int:
    base = _base_url()
    if not base:
        print("error: AUTO_ASSIGN_BASE_URL not set", file=__import__("sys").stderr)
        return 1

    cmd = args.aaw_command
    if cmd == "poll":
        results = poll_cycle(base)
        if getattr(args, "json", False):
            print(json.dumps(results, indent=2))
        else:
            print(f"processed {len(results)} assignments")
            for r in results:
                print(f"  {r['assignment_id']}: {r['status']}")
        return 0

    elif cmd == "start":
        interval = getattr(args, "interval", POLL_INTERVAL)
        once = getattr(args, "once", False)
        logger.info("auto-assign worker starting (interval=%ds)", interval)
        while True:
            try:
                results = poll_cycle(base)
                logger.info("cycle complete: %d assignments processed", len(results))
            except Exception as e:
                logger.error("poll cycle failed: %s", e)
            if once:
                break
            time.sleep(interval)
        return 0

    print("usage: hermes auto-assign-worker {start,poll}", file=__import__("sys").stderr)
    return 1
