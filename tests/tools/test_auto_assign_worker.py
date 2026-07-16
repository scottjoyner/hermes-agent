"""Tests for the auto-assign worker (hermes auto-assign-worker)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def worker_env(monkeypatch):
    monkeypatch.setenv("AUTO_ASSIGN_BASE_URL", "http://assign:8090")
    monkeypatch.setenv("HERMES_WORKER_ID", "test-worker")
    return monkeypatch


def test_fetch_recommended_assignments(worker_env):
    from hermes_cli.auto_assign_worker import fetch_recommended_assignments

    with patch("hermes_cli.auto_assign_worker.httpx.Client") as mock_client:
        mock_resp = mock_client.return_value.__enter__.return_value.get.return_value
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = [
            {"assignment_id": "a1", "task_id": "t1", "status": "recommended",
             "selected_lane": "direct_worker", "title": "task 1"},
            {"assignment_id": "a2", "task_id": "t2", "status": "recommended",
             "selected_lane": "local_only", "title": "task 2"},
            {"assignment_id": "a3", "task_id": "t3", "status": "running",
             "selected_lane": "direct_worker", "title": "task 3"},
        ]

        result = fetch_recommended_assignments("http://assign:8090", limit=10)

    assert len(result) == 2
    assert result[0]["assignment_id"] == "a1"
    assert result[1]["assignment_id"] == "a2"


def test_claim_assignment(worker_env):
    from hermes_cli.auto_assign_worker import claim_assignment

    with patch("hermes_cli.auto_assign_worker.httpx.Client") as mock_client:
        mock_resp = mock_client.return_value.__enter__.return_value.post.return_value
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"claim_id": "c1", "lease_expires_at": "2026-01-01T00:00:00Z"}

        result = claim_assignment("http://assign:8090", {
            "assignment_id": "a1", "task_id": "t1",
        })

    assert result["claim_id"] == "c1"
    mock_client.return_value.__enter__.return_value.post.assert_called_once()
    call_url = mock_client.return_value.__enter__.return_value.post.call_args[0][0]
    assert "/api/assignments/a1/claim" in call_url


def test_send_heartbeat(worker_env):
    from hermes_cli.auto_assign_worker import send_heartbeat

    with patch("hermes_cli.auto_assign_worker.httpx.Client") as mock_client:
        mock_resp = mock_client.return_value.__enter__.return_value.post.return_value
        mock_resp.raise_for_status.return_value = None

        result = send_heartbeat("http://assign:8090", "a1", "running")

    assert result is True


def test_complete_assignment(worker_env):
    from hermes_cli.auto_assign_worker import complete_assignment

    with patch("hermes_cli.auto_assign_worker.httpx.Client") as mock_client:
        mock_resp = mock_client.return_value.__enter__.return_value.post.return_value
        mock_resp.raise_for_status.return_value = None

        result = complete_assignment("http://assign:8090", "a1", "t1", "success", "done!")

    assert result is True
    call_body = mock_client.return_value.__enter__.return_value.post.call_args[1]["json"]
    assert call_body["status"] == "success"
    assert call_body["summary"] == "done!"


def test_poll_cycle(worker_env):
    from hermes_cli.auto_assign_worker import poll_cycle

    recommended = [
        {"assignment_id": "a1", "task_id": "t1", "status": "recommended",
         "selected_lane": "direct_worker", "title": "task 1"},
    ]

    with patch.multiple(
        "hermes_cli.auto_assign_worker",
        fetch_recommended_assignments= lambda base, limit=10: recommended,
        claim_assignment= lambda base, assignment: {"claim_id": "c1"},
        send_heartbeat= lambda base, aid, status="running": True,
        execute_work= lambda assignment: ("success", "done"),
        complete_assignment= lambda base, aid, tid, status="success", summary="": True,
    ):
        results = poll_cycle("http://assign:8090")

    assert len(results) == 1
    assert results[0]["assignment_id"] == "a1"
    assert results[0]["status"] == "success"


def test_poll_cycle_handles_failure(worker_env):
    from hermes_cli.auto_assign_worker import poll_cycle

    recommended = [
        {"assignment_id": "a2", "task_id": "t2", "status": "recommended",
         "selected_lane": "direct_worker", "title": "fail task"},
    ]

    def failing_claim(base, assignment):
        raise Exception("claim rejected")

    with patch.multiple(
        "hermes_cli.auto_assign_worker",
        fetch_recommended_assignments= lambda base, limit=10: recommended,
        claim_assignment= failing_claim,
    ):
        results = poll_cycle("http://assign:8090")

    assert len(results) == 1
    assert results[0]["assignment_id"] == "a2"
    assert results[0]["status"] == "failed"
    assert "claim rejected" in results[0]["error"]


def test_worker_id_default(worker_env):
    from hermes_cli.auto_assign_worker import _worker_id

    assert _worker_id() == "test-worker"


def test_worker_id_fallback(monkeypatch):
    monkeypatch.delenv("HERMES_WORKER_ID", raising=False)
    from hermes_cli.auto_assign_worker import _worker_id

    assert _worker_id() == "hermes-agent"


def test_base_url_from_env(worker_env):
    from hermes_cli.auto_assign_worker import _base_url

    assert _base_url() == "http://assign:8090"


def test_base_url_none(monkeypatch):
    monkeypatch.delenv("AUTO_ASSIGN_BASE_URL", raising=False)
    from hermes_cli.auto_assign_worker import _base_url

    assert _base_url() is None
