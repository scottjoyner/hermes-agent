"""Synthetic tests for the durable trace/receipt journal.

All payloads are synthetic.  No network, no real credentials, no
production paths — the journal is exercised against tmp_path only.
"""

import json
import os
import stat

import pytest

from agent.trace_receipts import (
    MAX_PAYLOAD_BYTES,
    TRACE_RECEIPT_SCHEMA,
    TraceReceiptError,
    TraceReceiptJournal,
    verify_journal,
)


def _journal(tmp_path):
    return TraceReceiptJournal(tmp_path / "traces" / "trace_receipts.jsonl")


def test_record_assigns_source_identity_and_sequence(tmp_path):
    journal = _journal(tmp_path)
    receipt = journal.record(
        "tool.call",
        {"tool": "terminal", "status": "ok"},
        source="hermes.observer",
        session_id="synthetic-session",
        turn_id="synthetic-turn",
    )
    assert receipt["source"] == "hermes.observer"
    assert receipt["kind"] == "tool.call"
    assert receipt["sequence"] == 1
    assert receipt["schema"] == TRACE_RECEIPT_SCHEMA
    assert receipt["prev_receipt_id"] is None
    assert len(receipt["receipt_id"]) == 64


def test_sequence_is_monotonic_per_source(tmp_path):
    journal = _journal(tmp_path)
    first = journal.record("turn.start", {"n": 1}, source="hermes.observer")
    second = journal.record("turn.end", {"n": 2}, source="hermes.observer")
    other = journal.record("turn.start", {"n": 1}, source="gateway.delivery_ledger")
    assert (first["sequence"], second["sequence"]) == (1, 2)
    # A different source gets its own independent sequence space.
    assert other["sequence"] == 1


def test_sequence_survives_reopen(tmp_path):
    journal = _journal(tmp_path)
    journal.record("turn.start", {"n": 1}, source="hermes.observer")
    reopened = _journal(tmp_path)
    receipt = reopened.record("turn.end", {"n": 2}, source="hermes.observer")
    assert receipt["sequence"] == 2
    assert receipt["prev_receipt_id"] is not None


def test_missing_source_fails_closed_and_writes_nothing(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(TraceReceiptError):
        journal.record("turn.start", {"n": 1}, source="")
    with pytest.raises(TraceReceiptError):
        journal.record("turn.start", {"n": 1}, source=None)  # type: ignore[arg-type]
    assert not journal.path.exists()


@pytest.mark.parametrize("source", ["has space", "bad/slash", "-leading", "x" * 65, "semi;colon"])
def test_invalid_source_rejected(tmp_path, source):
    journal = _journal(tmp_path)
    with pytest.raises(TraceReceiptError):
        journal.record("turn.start", {"n": 1}, source=source)
    assert not journal.path.exists()


def test_missing_kind_rejected(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(TraceReceiptError):
        journal.record("", {"n": 1}, source="hermes.observer")
    assert not journal.path.exists()


def test_payload_must_be_a_json_object(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(TraceReceiptError):
        journal.record("turn.start", ["not", "a", "dict"], source="hermes.observer")  # type: ignore[arg-type]
    assert not journal.path.exists()


def test_payload_must_be_json_serializable(tmp_path):
    journal = _journal(tmp_path)
    with pytest.raises(TraceReceiptError):
        journal.record("turn.start", {"bad": object()}, source="hermes.observer")
    assert not journal.path.exists()


def test_oversized_payload_rejected(tmp_path):
    journal = _journal(tmp_path)
    big = {"blob": "x" * (MAX_PAYLOAD_BYTES + 1)}
    with pytest.raises(TraceReceiptError):
        journal.record("turn.start", big, source="hermes.observer")
    assert not journal.path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"password": "synthetic"},
        {"nested": {"client_secret": "synthetic"}},
        {"headers": [{"Authorization": "Bearer synthetic"}]},
        {"api_key": "synthetic"},
        {"deep": [{"a": [{"session_token": "synthetic"}]}]},
        {"cookie": "synthetic"},
    ],
)
def test_credential_like_keys_rejected_fail_closed(tmp_path, payload):
    journal = _journal(tmp_path)
    with pytest.raises(TraceReceiptError):
        journal.record("turn.start", payload, source="hermes.observer")
    assert not journal.path.exists()


def test_receipt_id_recomputes_and_chain_verifies(tmp_path):
    journal = _journal(tmp_path)
    journal.record("turn.start", {"n": 1}, source="hermes.observer")
    journal.record("turn.end", {"n": 2}, source="hermes.observer")
    summary = verify_journal(journal.path)
    assert summary["records"] == 2
    assert summary["sources"] == {"hermes.observer": 2}


def test_tampered_payload_is_detected(tmp_path):
    journal = _journal(tmp_path)
    journal.record("turn.start", {"n": 1}, source="hermes.observer")
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"]["n"] = 999  # tamper without rehashing
    journal.path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(TraceReceiptError):
        verify_journal(journal.path)


def test_truncated_chain_is_detected(tmp_path):
    journal = _journal(tmp_path)
    journal.record("turn.start", {"n": 1}, source="hermes.observer")
    journal.record("turn.end", {"n": 2}, source="hermes.observer")
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    journal.path.write_text(lines[1] + "\n", encoding="utf-8")  # drop first record
    with pytest.raises(TraceReceiptError):
        verify_journal(journal.path)


def test_corrupt_line_fails_closed_on_append(tmp_path):
    journal = _journal(tmp_path)
    journal.record("turn.start", {"n": 1}, source="hermes.observer")
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{not valid json\n")
    with pytest.raises(TraceReceiptError):
        journal.record("turn.end", {"n": 2}, source="hermes.observer")


def test_journal_created_with_owner_only_permissions(tmp_path):
    journal = _journal(tmp_path)
    journal.record("turn.start", {"n": 1}, source="hermes.observer")
    mode = stat.S_IMODE(os.stat(journal.path).st_mode)
    assert mode == 0o600


def test_verify_empty_journal_is_ok(tmp_path):
    missing = tmp_path / "traces" / "nope.jsonl"
    summary = verify_journal(missing)
    assert summary == {"records": 0, "sources": {}, "path": str(missing)}


def test_schema_change_fails_closed(tmp_path):
    journal = _journal(tmp_path)
    journal.record("turn.start", {"n": 1}, source="hermes.observer")
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["schema"] = "hermes.trace_receipt.v999"
    journal.path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(TraceReceiptError):
        verify_journal(journal.path)



def test_symlink_journal_rejected_without_touching_target(tmp_path):
    target = tmp_path / "unrelated.txt"
    target.write_text("synthetic sentinel", encoding="utf-8")
    journal = _journal(tmp_path)
    journal.path.parent.mkdir()
    journal.path.symlink_to(target)
    with pytest.raises(TraceReceiptError, match="symlink"):
        verify_journal(journal.path)
    with pytest.raises(TraceReceiptError, match="symlink"):
        journal.record("turn.start", {"n": 1}, source="hermes.observer")
    assert target.read_text(encoding="utf-8") == "synthetic sentinel"


def test_symlink_journal_parent_rejected(tmp_path):
    actual_directory = tmp_path / "other"
    actual_directory.mkdir()
    (tmp_path / "traces").symlink_to(actual_directory, target_is_directory=True)
    journal = _journal(tmp_path)
    with pytest.raises(TraceReceiptError, match="symlink"):
        verify_journal(journal.path)
    with pytest.raises(TraceReceiptError, match="symlink"):
        journal.record("turn.start", {"n": 1}, source="hermes.observer")
    assert list(actual_directory.iterdir()) == []


def test_short_os_writes_are_completed_before_fsync(tmp_path, monkeypatch):
    real_write = os.write
    def write_short(fd, chunk):
        return real_write(fd, chunk[:7])
    monkeypatch.setattr(os, "write", write_short)
    journal = _journal(tmp_path)
    receipt = journal.record("turn.start", {"n": 1}, source="hermes.observer")
    assert receipt["sequence"] == 1
    assert verify_journal(journal.path)["records"] == 1


def test_zero_progress_write_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "write", lambda _fd, _chunk: 0)
    journal = _journal(tmp_path)
    with pytest.raises(TraceReceiptError, match="append failed"):
        journal.record("turn.start", {"n": 1}, source="hermes.observer")
    assert verify_journal(journal.path)["records"] == 0
