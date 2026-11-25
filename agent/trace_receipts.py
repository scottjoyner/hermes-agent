"""Durable, fail-closed trace/receipt journal for Hermes observability.

A trace record is only useful to an audit/replay pipeline if it can answer
two questions without trusting the writer: *who emitted this* (source
identity) and *in what order* (per-source sequence).  Most ad-hoc
telemetry dumps answer neither, and a tampered or corrupted stream is
indistinguishable from a healthy one.

This module provides a small append-only journal that:

- requires an explicit, validated ``source`` identity on every record
  (fail-closed: missing/invalid provenance rejects the write);
- assigns a monotonic per-source ``sequence`` number that survives
  process restarts (rebuilt by scanning the existing journal);
- hash-chains records (``prev_receipt_id``) and content-hashes payloads
  (``payload_sha256``) so interior corruption, missing prefixes and
  broken links are detected by :func:`verify_journal`. A coherent prefix
  remains valid after *tail deletion*, and an attacker able to rewrite the
  entire file can recompute this unkeyed chain. Completeness, authenticity
  and anti-rollback need a separately trusted pinned head/receipt;
- rejects payloads that are not JSON objects, exceed a bounded size, or
  carry credential-looking keys anywhere in the tree (fail-closed
  payload safety, mirroring the observer contract's redaction stance);
- writes with ``fsync`` and ``0600`` permissions — trace data may carry
  session metadata and must not leak.

Scope boundary: this module is the durable journal primitive only.  It is
intentionally NOT wired into the agent loop, gateway, or observer hooks —
no production path writes to it, and no caller is silently migrated.  Wire
it up only behind an explicit, reviewed integration with its own rollback
plan.

Nothing here performs network I/O, reads credentials, or mutates live
configuration.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import logging
import os
import re
import threading
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no POSIX flock
    fcntl = None
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

TRACE_RECEIPT_SCHEMA = "hermes.trace_receipt.v1"

# Source identity: dotted/slug-like tokens, e.g. "hermes.observer",
# "gateway.delivery_ledger", "plugin.langfuse".  Deliberately strict —
# a source that needs escaping is not an identity, it's a payload.
_SOURCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,63}$")

_KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,63}$")

# Payload bounds.  Trace payloads are observability hints, not bulk data.
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_JOURNAL_BYTES = 16 * 1024 * 1024

# Credential-looking key fragments (compared case-insensitively against
# each key with separators stripped).  Fail-closed: any match rejects the
# whole record rather than silently dropping a field — a partially written
# receipt is worse than a rejected one.
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "apicredential",
    "credential",
    "privatekey",
    "accesskey",
    "clientsecret",
    "cookie",
)

_LOCK = threading.Lock()


class TraceReceiptError(Exception):
    """Raised when a record (or the journal itself) fails closed."""


def _refuse_path_symlinks(path: Path) -> None:
    """Reject symlink components, not just the journal and its direct parent.

    This is a static safety check, not a defense against concurrent ancestor
    swaps; a production integration also needs trusted directory custody.
    """
    if ".." in path.parts:
        raise TraceReceiptError("trace receipt journal parent traversal refused")
    absolute = Path(os.path.abspath(path))  # Do not resolve symlink components.
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise TraceReceiptError("trace receipt journal symlink redirection refused")


@contextmanager
def _journal_process_lock(path: Path) -> Iterator[None]:
    """Bounded cross-process lock covering verify/sequence calculation/append.

    The lock is separate from the JSONL leaf so opening it cannot truncate or
    mutate receipts. A symlink lock leaf or journal directory fails closed.
    Crash exits release POSIX flock automatically; Windows refuses writes
    until a separately reviewed native lock implementation exists.
    """
    if fcntl is None:
        raise TraceReceiptError("POSIX journal process lock unavailable")
    _refuse_path_symlinks(path)
    lock_path = path.with_name(path.name + ".lock")
    if lock_path.is_symlink():
        raise TraceReceiptError("trace receipt journal lock symlink refused")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise TraceReceiptError("trace receipt journal lock could not be opened") from exc
    acquired = False
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TraceReceiptError("trace receipt journal lock timed out") from exc
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def default_journal_path() -> Path:
    return get_hermes_home() / "traces" / "trace_receipts.jsonl"


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _check_no_sensitive_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = _normalize_key(key)
            for fragment in _SENSITIVE_KEY_FRAGMENTS:
                if fragment in normalized:
                    raise TraceReceiptError(
                        f"payload key at {path}.{key} looks credential-like; "
                        "refusing to record (fail-closed)"
                    )
            _check_no_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_no_sensitive_keys(item, f"{path}[{index}]")


def _validate_source(source: Any) -> str:
    if not isinstance(source, str):
        raise TraceReceiptError("source identity must be a string")
    candidate = source.strip()
    if not candidate:
        raise TraceReceiptError("source identity is required (fail-closed)")
    if not _SOURCE_RE.match(candidate):
        raise TraceReceiptError(
            f"source identity {candidate!r} does not match "
            "^[A-Za-z][A-Za-z0-9_.-]{0,63}$"
        )
    return candidate


def _validate_kind(kind: Any) -> str:
    if not isinstance(kind, str):
        raise TraceReceiptError("kind must be a string")
    candidate = kind.strip()
    if not candidate:
        raise TraceReceiptError("kind is required (fail-closed)")
    if not _KIND_RE.match(candidate):
        raise TraceReceiptError(
            f"kind {candidate!r} does not match ^[A-Za-z][A-Za-z0-9_.-]{{0,63}}$"
        )
    return candidate


def _validate_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise TraceReceiptError("payload must be a JSON object (dict)")
    try:
        encoded = _canonical_json(payload)
    except (TypeError, ValueError) as exc:
        raise TraceReceiptError(f"payload is not JSON-serializable: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise TraceReceiptError(
            f"payload exceeds {MAX_PAYLOAD_BYTES} bytes; refusing to record"
        )
    _check_no_sensitive_keys(payload)
    return payload


def _record_hash_input(record: Dict[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key != "receipt_id"}
    return _canonical_json(body)


def _iter_lines(path: Path) -> Iterator[Tuple[int, str]]:
    with path.open("r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            line = raw.strip()
            if line:
                yield lineno, line


def _parse_and_check_record(line: str, lineno: int) -> Dict[str, Any]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise TraceReceiptError(
            f"journal line {lineno} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(record, dict):
        raise TraceReceiptError(f"journal line {lineno} is not a JSON object")
    if record.get("schema") != TRACE_RECEIPT_SCHEMA:
        raise TraceReceiptError(
            f"journal line {lineno} has unexpected schema {record.get('schema')!r}"
        )
    payload_sha = record.get("payload_sha256")
    payload = record.get("payload")
    if not isinstance(payload_sha, str) or not isinstance(payload, dict):
        raise TraceReceiptError(f"journal line {lineno} is missing payload fields")
    if _sha256_hex(_canonical_json(payload)) != payload_sha:
        raise TraceReceiptError(f"journal line {lineno} payload hash mismatch")
    expected_id = _sha256_hex(_record_hash_input(record))
    if record.get("receipt_id") != expected_id:
        raise TraceReceiptError(f"journal line {lineno} receipt_id mismatch")
    return record


def verify_journal(path: Path) -> Dict[str, Any]:
    """Verify chain integrity of an existing journal.

    Returns a summary dict.  Raises :class:`TraceReceiptError` on any
    corruption — callers must treat that as fail-closed (do not record
    further, do not trust prior contents).
    """
    path = Path(path)
    # Never follow an attacker-controlled journal leaf or immediate directory
    # symlink; both would redirect audit reads/writes away from the requested
    # storage path. O_NOFOLLOW also guards the leaf at the actual open() call.
    _refuse_path_symlinks(path)
    if not path.exists():
        return {"records": 0, "sources": {}, "path": str(path)}
    sequences: Dict[str, int] = {}
    prev_id: Optional[str] = None
    count = 0
    for lineno, line in _iter_lines(path):
        record = _parse_and_check_record(line, lineno)
        if record.get("prev_receipt_id") != prev_id:
            raise TraceReceiptError(
                f"journal line {lineno} breaks the receipt chain "
                f"(expected prev_receipt_id={prev_id!r})"
            )
        source = record.get("source")
        if not isinstance(source, str):
            raise TraceReceiptError(f"journal line {lineno} is missing source")
        sequence = record.get("sequence")
        expected_sequence = sequences.get(source, 0) + 1
        if sequence != expected_sequence:
            raise TraceReceiptError(
                f"journal line {lineno} has sequence {sequence!r} for source "
                f"{source!r}; expected {expected_sequence}"
            )
        sequences[source] = sequence
        prev_id = record["receipt_id"]
        count += 1
    return {"records": count, "sources": sequences, "path": str(path)}


class TraceReceiptJournal:
    """Append-only trace/receipt journal rooted at a single JSONL file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_journal_path()
        self._sequences: Dict[str, int] = {}
        self._prev_receipt_id: Optional[str] = None

    # -- internal helpers -------------------------------------------------
    def _load(self) -> None:
        # Re-verify from disk on every record: external corruption/tampering
        # must fail closed even for a long-lived journal object.  The
        # journal is capped at MAX_JOURNAL_BYTES, so this stays cheap.
        self._sequences = {}
        self._prev_receipt_id = None
        _refuse_path_symlinks(self.path)
        if self.path.exists():
            size = self.path.stat().st_size
            if size > MAX_JOURNAL_BYTES:
                raise TraceReceiptError(
                    f"journal exceeds {MAX_JOURNAL_BYTES} bytes; refusing to append"
                )
            summary = verify_journal(self.path)
            self._sequences = dict(summary["sources"])
            if summary["records"]:
                last_line = None
                for _, line in _iter_lines(self.path):
                    last_line = line
                if last_line is not None:
                    self._prev_receipt_id = json.loads(last_line)["receipt_id"]

    def record(
        self,
        kind: str,
        payload: Dict[str, Any],
        *,
        source: str,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate, append and return a receipt for one trace event.

        Raises :class:`TraceReceiptError` (and writes nothing) when
        provenance or payload safety fails closed.
        """
        with _LOCK, _journal_process_lock(self.path):
            _refuse_path_symlinks(self.path)
            self._load()
            source = _validate_source(source)
            kind = _validate_kind(kind)
            payload = _validate_payload(payload)
            sequence = self._sequences.get(source, 0) + 1
            record: Dict[str, Any] = {
                "schema": TRACE_RECEIPT_SCHEMA,
                "source": source,
                "kind": kind,
                "session_id": session_id,
                "turn_id": turn_id,
                "sequence": sequence,
                "recorded_at": time.time(),
                "payload": payload,
                "payload_sha256": _sha256_hex(_canonical_json(payload)),
                "prev_receipt_id": self._prev_receipt_id,
            }
            record["receipt_id"] = _sha256_hex(_record_hash_input(record))
            line = _canonical_json(record) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    # Secure the actual opened inode, not a path that could be
                    # swapped to a symlink between write and chmod.
                    os.fchmod(fd, 0o600)
                    remaining = memoryview(line.encode("utf-8"))
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError("journal write made no progress")
                        remaining = remaining[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise TraceReceiptError("trace receipt journal append failed") from exc
            self._sequences[source] = sequence
            self._prev_receipt_id = record["receipt_id"]
            return {
                "schema": TRACE_RECEIPT_SCHEMA,
                "receipt_id": record["receipt_id"],
                "source": source,
                "kind": kind,
                "sequence": sequence,
                "recorded_at": record["recorded_at"],
                "payload_sha256": record["payload_sha256"],
                "prev_receipt_id": record["prev_receipt_id"],
            }

    def records_for_source(self, source: str) -> List[Dict[str, Any]]:
        """Return parsed records for one source (verification-grade read)."""
        with _LOCK, _journal_process_lock(self.path):
            self._load()
            source = _validate_source(source)
            if not self.path.exists():
                return []
            verify_journal(self.path)
            parsed = [json.loads(line) for _, line in _iter_lines(self.path)]
            return [record for record in parsed if record.get("source") == source]
