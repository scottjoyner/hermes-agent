"""Durable SQLite write-behind queue for the knowledge graph provider."""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class DurableQueue:
    """Delete rows only after the remote callback succeeds."""

    _STOP = object()

    def __init__(
        self,
        path: Path,
        callback: Callable[[Dict[str, Any]], None],
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.callback = callback
        self._work: "queue.Queue[Any]" = queue.Queue()
        self._stopping = threading.Event()
        self._init_db()
        self._thread = threading.Thread(
            target=self._run,
            name="hermes-kg-writer",
            daemon=True,
        )
        self._thread.start()
        for row_id in self._pending_ids():
            self._work.put(row_id)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )

    def _pending_ids(self) -> List[int]:
        with self._connect() as conn:
            return [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM pending ORDER BY id"
                )
            ]

    def enqueue(self, payload: Dict[str, Any]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO pending(payload, created_at) VALUES (?, ?)",
                (json.dumps(payload, ensure_ascii=False), time.time()),
            )
            row_id = int(cursor.lastrowid)
        self._work.put(row_id)
        return row_id

    def _load(self, row_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM pending WHERE id = ?",
                (row_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def _mark_success(self, row_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending WHERE id = ?", (row_id,))

    def _mark_failure(self, row_id: int) -> int:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pending SET attempts = attempts + 1 WHERE id = ?",
                (row_id,),
            )
            row = conn.execute(
                "SELECT attempts FROM pending WHERE id = ?",
                (row_id,),
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def _run(self) -> None:
        while not self._stopping.is_set():
            item = self._work.get()
            if item is self._STOP:
                return
            payload = self._load(int(item))
            if payload is None:
                continue
            try:
                self.callback(payload)
            except Exception as exc:
                attempts = self._mark_failure(int(item))
                logger.warning(
                    "Knowledge graph write failed (attempt %s): %s",
                    attempts,
                    exc,
                )
                delay = min(30.0, max(1.0, 2.0 ** min(attempts, 5)))
                if not self._stopping.wait(delay):
                    self._work.put(item)
            else:
                self._mark_success(int(item))

    def pending_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM pending"
            ).fetchone()
        return int(row["n"])

    def close(self, timeout: float = 10.0) -> None:
        deadline = time.time() + max(0.0, timeout)
        while self.pending_count() and time.time() < deadline:
            time.sleep(0.1)
        self._stopping.set()
        self._work.put(self._STOP)
        self._thread.join(timeout=2.0)
