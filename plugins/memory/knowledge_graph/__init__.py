"""Knowledge Graph memory provider — Hermes' "second brain".

Captures EVERY Hermes session as a graph in Neo4j:

    (Session)-[:HAS]->(Message|Reasoning|ToolCall|Idea|Entity)
    (Message:user)-[:FOLLOWED_BY]->(Message:assistant)
    (Message:assistant)-[:REASONED]->(Reasoning)        # chain-of-thought
    (Message:assistant)-[:CALLED]->(ToolCall)-[:PRODUCED]->(Message:tool_result)
    (Idea)-[:LINKS_TO]->(Entity {doc|repo|filepath|url|concept})

Every searchable node is embedded with a LOCAL embedding model (LM Studio /
Ollama OpenAI-compatible endpoint) and indexed by Neo4j's native vector index,
so recall is BOTH semantic (vector similarity) AND relational (graph traversal) —
strictly richer than flat memory providers. A dedicated ``Idea`` node type links
loose thoughts to docs, repos, and file paths, forming a navigable idea graph.

Capture is fully asynchronous and crash-safe: turns are enqueued to a durable
SQLite write-behind queue and flushed by a background worker, so a slow or
down Neo4j never blocks the user.

Activate with ``memory.provider: knowledge_graph`` in config.yaml (or
``hermes memory setup``).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Local imports (kept lazy where they pull native deps).
from .embeddings import LocalEmbeddingClient
from .graph_store import Neo4jGraphStore


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

KG_SEARCH_SCHEMA = {
    "name": "kg_search",
    "description": (
        "Hybrid semantic + graph search across the knowledge graph (all past "
        "sessions, chain-of-thought, tool calls, ideas, and indexed docs). "
        "Re-ranks a MIX of doc and chain-of-thought hits. Use scope to "
        "favor docs, CoT, or both, and doc_weight/cot_weight to blend."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 8, max: 30)."},
            "scope": {
                "type": "string", "enum": ["both", "docs", "cot"],
                "description": "Blend: both (default), docs only, or chain-of-thought only.",
            },
            "doc_weight": {"type": "number", "description": "Doc blend weight when scope=both (default: 0.6)."},
            "cot_weight": {"type": "number", "description": "CoT blend weight when scope=both (default: 0.4)."},
            "expand_graph": {
                "type": "boolean",
                "description": "Also return immediate graph neighbors of each hit (default: false).",
            },
        },
        "required": ["query"],
    },
}

KG_QUERY_SCHEMA = {
    "name": "kg_query",
    "description": (
        "Run a raw Cypher query against the knowledge graph for advanced "
        "traversal/aggregation. Returns rows as JSON. Use for graph analytics "
        "the other tools don't cover."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cypher": {"type": "string", "description": "Cypher query (read-only recommended)."},
            "limit": {"type": "integer", "description": "Row cap (default: 50)."},
        },
        "required": ["cypher"],
    },
}

KG_RELATED_SCHEMA = {
    "name": "kg_related",
    "description": (
        "Find what a node links to in the graph — e.g. an idea's linked docs/"
        "repos/file paths, or a tool call's inputs and outputs. Pass a node id "
        "(from kg_search) or free text to locate the nearest node first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Node id from kg_search (omit to match by text)."},
            "text": {"type": "string", "description": "Free text to locate the nearest node when node_id is omitted."},
            "rel_types": {
                "type": "array", "items": {"type": "string"},
                "description": "Restrict relationship types (e.g. LINKS_TO, CALLED, REASONED).",
            },
            "limit": {"type": "integer", "description": "Max neighbors (default: 30)."},
        },
        "required": [],
    },
}

KG_REMEMBER_IDEA_SCHEMA = {
    "name": "kg_remember_idea",
    "description": (
        "Record an idea/insight as a first-class knowledge-graph node and link it "
        "to related things: docs, repos, file paths, URLs, or concepts. Ideas "
        "form a traversable subgraph distinct from raw session history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The idea / insight text."},
            "tags": {
                "type": "array", "items": {"type": "string"},
                "description": "Optional tags for the idea.",
            },
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["doc", "repo", "filepath", "url", "concept", "idea"],
                            "description": "What the link points at.",
                        },
                        "value": {"type": "string", "description": "Path / URL / name / concept text."},
                        "note": {"type": "string", "description": "Optional relationship note."},
                    },
                    "required": ["kind", "value"],
                },
                "description": "Things this idea links to.",
            },
        },
        "required": ["content"],
    },
}

KG_READ_DOC_SCHEMA = {
    "name": "kg_read_doc",
    "description": (
        "Resolve a knowledge-graph doc (by path or doc node id) to its REAL "
        "source file and return a pointer + snippet. ALWAYS open the full "
        "source with read_file before answering — the graph only summarizes docs, "
        "it is not a substitute for reading them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Local file path of the doc."},
            "doc_id": {"type": "string", "description": "Doc node id from kg_search (omit if path given)."},
            "snippet_chars": {"type": "integer", "description": "Snippet length (default: 600)."},
        },
        "required": [],
    },
}

KG_INDEX_DOCS_SCHEMA = {
    "name": "kg_index_docs",
    "description": (
        "Index markdown/text docs into the knowledge graph as embeddable "
        "DocChunk nodes linked to their real file path. This is the agent's "
        "doc surface — recall points at the SOURCE file, not a graph summary. "
        "Returns chunk counts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "paths": {
                "type": "array", "items": {"type": "string"},
                "description": "Files or directories to index (.md/.txt).",
            },
            "recursive": {"type": "boolean", "description": "Recurse into directories (default: true)."},
            "glob": {"type": "string", "description": "File glob within dirs (default: *.md)."},
        },
        "required": ["paths"],
    },
}

KG_FORGET_SCHEMA = {
    "name": "kg_forget",
    "description": "Delete a knowledge-graph node (and its relationships) by id.",
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Node id to delete."},
        },
        "required": ["node_id"],
    },
}

KG_IMPORT_SESSIONS_SCHEMA = {
    "name": "kg_import_sessions",
    "description": (
        "Backfill/reconcile Hermes SQLite state.db sessions into Neo4j. "
        "Idempotent: stable node ids are derived from SQLite row ids. "
        "Use dry_run first to review counts before writing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "db_path": {"type": "string", "description": "Path to state.db (default: $HERMES_HOME/state.db)."},
            "dry_run": {"type": "boolean", "description": "Parse and count without writing (default: true)."},
            "since_ts": {"type": "number", "description": "Only sessions with started_at >= this Unix timestamp."},
            "limit_sessions": {"type": "integer", "description": "Optional cap for testing/incremental imports."},
            "session_ids": {"type": "array", "items": {"type": "string"}, "description": "Explicit session ids to import."},
            "embed_tools": {"type": "boolean", "description": "Also embed tool calls/results (default false; graph-only otherwise)."},
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Durable write-behind queue
# ---------------------------------------------------------------------------

class _CaptureQueue:
    """SQLite-backed async capture queue (mirrors retaindb's pattern)."""

    _SHUTDOWN = object()

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._q: "queue.Queue[Any]" = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="kg-writer", daemon=True)
        self._local = threading.local()
        self._init_db()
        self._thread.start()
        # Replay any rows left from a previous crash.
        for row in self._pending_rows():
            self._q.put(json.loads(row))

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kg_pending ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "job_json TEXT, created_at TEXT, last_error TEXT)"
        )
        conn.commit()

    def _pending_rows(self) -> List[str]:
        conn = self._conn()
        return [
            r["job_json"] for r in
            conn.execute("SELECT job_json FROM kg_pending ORDER BY id ASC LIMIT 300").fetchall()
        ]

    def enqueue(self, session_id: str, job: Dict[str, Any]) -> None:
        # Accept either a dict (normal path) or an already-serialized string.
        if isinstance(job, (dict, list)):
            job_json = json.dumps(job, ensure_ascii=False)
        else:
            job_json = str(job)
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO kg_pending (session_id, job_json, created_at) VALUES (?,?,?)",
                (session_id, job_json, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("kg enqueue insert failed: %s", exc)
            return
        self._q.put(job_json)

    def _flush(self, job_json: str) -> None:
        conn = self._conn()
        try:
            yield_job = json.loads(job_json)
            self._worker(yield_job)
            conn.execute("DELETE FROM kg_pending WHERE job_json = ?", (job_json,))
            conn.commit()
        except Exception as exc:
            logger.warning("kg capture flush failed (will retry): %s", exc)
            try:
                conn.execute("UPDATE kg_pending SET last_error = ? WHERE job_json = ?",
                            (str(exc), job_json))
                conn.commit()
            except Exception:
                pass
            # Only retry transient failures a bounded number of times so a
            # permanently-broken job can't spin the writer thread forever.
            retries = getattr(self, "_retry_count", {})
            retries[job_json] = retries.get(job_json, 0) + 1
            self._retry_count = retries
            if retries[job_json] <= 5:
                time.sleep(2)
                self._q.put(job_json)  # requeue for retry
            else:
                logger.error("kg job dropped after 5 failed flushes: %s", exc)

    def _worker(self, job: Dict[str, Any]) -> None:
        # Injected by the provider so the queue stays storage-agnostic.
        raise NotImplementedError

    def _loop(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=5)
                if item is self._SHUTDOWN:
                    break
                self._flush(item)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.error("kg writer error: %s", exc)

    def shutdown(self) -> None:
        self._q.put(self._SHUTDOWN)
        self._thread.join(timeout=10)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeGraphMemoryProvider(MemoryProvider):
    """Neo4j knowledge-graph "second brain"."""

    def __init__(self) -> None:
        self._store: Optional[Neo4jGraphStore] = None
        self._embed: Optional[LocalEmbeddingClient] = None
        self._queue: Optional[_CaptureQueue] = None
        self._session_id = ""
        self._profile = ""
        self._platform = ""
        self._cfg: Dict[str, Any] = {}
        self._prefetch_result: str = ""
        self._prefetch_lock = threading.Lock()
        self._seq_cache: Dict[str, int] = {}
        self._seq_lock = threading.Lock()
        self._available = False
        self._model = ""

    # -- identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "knowledge_graph"

    # -- config --------------------------------------------------------------

    def _load_config(self) -> Dict[str, Any]:
        try:
            from hermes_cli.config import load_config, cfg_get

            config = load_config()
            section = cfg_get(config, "knowledge_graph") or {}
        except Exception:
            section = {}
        # Defaults (kept in sync with hermes_cli/config.py DEFAULT_CONFIG).
        defaults = {
            "enabled": False,
            "uri": "", "user": "", "password": "", "database": "",
            "embeddings": {
                "backend": "local", "base_url": "", "model": "",
                "api_key": "", "dimensions": 0, "batch_size": 32, "timeout": 30.0,
            },
            "capture": {
                "sessions": True, "chain_of_thought": True,
                "tool_calls": True, "user_messages": True, "assistant_messages": True,
            },
            "search": {"top_k": 8, "similarity_cutoff": 0.0, "include_cot": True,
                       "include_docs": True, "scope": "both",
                       "doc_weight": 0.6, "cot_weight": 0.4},
            "doc_roots": [],
            "auto_index_docs": False,
            "harness": {
                "tier": "", "default_tier": "standard",
                "tiers": {
                    "weak":     {"inject_full_text": True,  "read_source_nudge": True,
                                "prefetch_top_k": 12, "max_recall_chars": 1200},
                    "standard": {"inject_full_text": False, "read_source_nudge": False,
                                 "prefetch_top_k": 8, "max_recall_chars": 600},
                    "strong":   {"inject_full_text": False, "read_source_nudge": False,
                                 "prefetch_top_k": 6, "max_recall_chars": 400},
                },
            },
        }
        merged = dict(defaults)
        merged.update(section or {})
        merged["embeddings"] = {**defaults["embeddings"], **(section.get("embeddings") or {})}
        merged["capture"] = {**defaults["capture"], **(section.get("capture") or {})}
        merged["search"] = {**defaults["search"], **(section.get("search") or {})}
        merged["harness"] = {**defaults["harness"], **(section.get("harness") or {})}
        return merged

    def _neo4j_params(self) -> Dict[str, str]:
        uri = self._cfg.get("uri") or os.environ.get("NEO4J_URI") or "bolt://localhost:7687"
        user = self._cfg.get("user") or os.environ.get("NEO4J_USER") or "neo4j"
        password = self._cfg.get("password") or os.environ.get("NEO4J_PASSWORD") or ""
        db = (self._cfg.get("database")
               or os.environ.get("NEO4J_DATABASE") or "neo4j")
        return {"uri": uri, "user": user, "password": password, "database": db}

    def is_available(self) -> bool:
        if not self._cfg:
            self._cfg = self._load_config()
        enabled = self._cfg.get("enabled") or bool(
            os.environ.get("NEO4J_URI") or os.environ.get("NEO4J_PASSWORD")
        )
        if not enabled:
            return False
        # Lightweight: we consider it available if we can build a client + resolve a URI.
        try:
            params = self._neo4j_params()
            if not params["uri"].startswith(("bolt://", "neo4j://", "http://", "https://")):
                return False
            return True
        except Exception:
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "uri", "description": "Neo4j bolt URI (e.g. bolt://localhost:7687)",
             "secret": False, "required": False, "env_var": "NEO4J_URI"},
            {"key": "user", "description": "Neo4j user", "secret": False,
             "required": False, "env_var": "NEO4J_USER"},
            {"key": "password", "description": "Neo4j password", "secret": True,
             "required": False, "env_var": "NEO4J_PASSWORD"},
            {"key": "database", "description": "Neo4j database name", "secret": False,
             "required": False, "env_var": "NEO4J_DATABASE"},
            {"key": "embeddings.base_url", "description": "Local embeddings base URL (OpenAI-compatible)",
             "secret": False, "required": False, "env_var": "LMSTUDIO_EMBEDDINGS_BASE_URL"},
            {"key": "embeddings.model", "description": "Embedding model name (e.g. nomic-embed-text)",
             "secret": False, "required": False, "env_var": "LMSTUDIO_EMBEDDINGS_MODEL"},
        ]

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._cfg = self._load_config()
        self._session_id = session_id or ""
        self._profile = os.path.basename(str(kwargs.get("hermes_home") or "")) or "default"
        self._platform = kwargs.get("platform") or "cli"
        self._model = str(kwargs.get("model") or "")

        # Embeddings client (local, OpenAI-compatible).
        emb = self._cfg.get("embeddings") or {}
        self._embed = LocalEmbeddingClient(
            base_url=emb.get("base_url") or "",
            model=emb.get("model") or "",
            api_key=emb.get("api_key") or "",
            timeout=float(emb.get("timeout") or 30.0),
            batch_size=int(emb.get("batch_size") or 32),
        )

        # Graph store.
        self._store = Neo4jGraphStore(**self._neo4j_params())
        if not self._store.connect():
            logger.warning("Knowledge-graph Neo4j unavailable — capture disabled.")
            self._available = False
            return
        self._available = True

        # Detect embedding dim up-front so the vector index exists early.
        dim = int(emb.get("dimensions") or 0) or None
        if dim is None:
            try:
                probe = self._embed.embed("knowledge graph init probe")
                dim = len(probe) if probe else None
            except Exception:
                dim = None
        self._store.ensure_schema(dim)

        # Durable capture queue.
        from hermes_constants import get_hermes_home

        db_path = get_hermes_home() / "knowledge_graph_queue.db"
        self._queue = _CaptureQueue(db_path)
        self._queue._worker = self._apply_job  # type: ignore[attr-defined]

        # Load seq counters from meta.
        for key, val in (self._store.get_meta("seq_cache") or {}).items():
            try:
                self._seq_cache[key] = int(val)
            except Exception:
                pass

        # Ensure session node exists.
        if self._cfg.get("capture", {}).get("sessions", True) and self._session_id:
            self._enqueue_session_job(self._session_id)

        # Auto-index configured doc roots at startup (idempotent MERGE).
        if self._cfg.get("auto_index_docs") and self._available:
            roots = [str(r) for r in (self._cfg.get("doc_roots") or [])]
            if roots:
                t = threading.Thread(
                    target=self._auto_index, args=(roots,),
                    name="kg-auto-index", daemon=True,
                )
                t.start()

    def _auto_index(self, roots: List[str]) -> None:
        try:
            self._tool_index_docs({"paths": roots, "recursive": True})
        except Exception as exc:
            logger.warning("kg auto-index failed: %s", exc)

    def _enqueue_job(self, session_id: str, job: Dict[str, Any]) -> None:
        if not self._queue or not self._available:
            return
        try:
            self._queue.enqueue(session_id, job)
        except Exception as exc:
            logger.debug("kg enqueue failed: %s", exc)

    def _enqueue_session_job(
        self,
        session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        event: str = "start",
        **props: Any,
    ) -> None:
        """Queue an idempotent KgSession upsert."""
        if not session_id or not self._cfg.get("capture", {}).get("sessions", True):
            return
        job = {
            "type": "session",
            "session_id": session_id,
            "platform": props.pop("platform", self._platform),
            "profile": props.pop("profile", self._profile),
            "model": props.pop("model", self._model),
            "parent_session_id": parent_session_id or "",
            "reset": bool(reset),
            "event": event,
            "props": props,
        }
        self._enqueue_job(session_id, job)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs: Any,
    ) -> None:
        """Refresh cached session state when AIAgent.session_id rotates."""
        if not new_session_id:
            return
        old_session_id = self._session_id
        self._session_id = new_session_id
        if reset:
            with self._seq_lock:
                self._seq_cache.pop(new_session_id, None)
        if not self._available or not self._cfg:
            return
        self._enqueue_session_job(
            new_session_id,
            parent_session_id=parent_session_id or old_session_id or "",
            reset=reset,
            event=str(kwargs.get("event") or kwargs.get("reason") or "switch"),
            platform=str(kwargs.get("platform") or self._platform or ""),
            profile=str(kwargs.get("profile") or self._profile or ""),
            model=str(kwargs.get("model") or self._model or ""),
        )

    # -- sequence helper ------------------------------------------------------

    def _next_seq(self, session_id: str) -> int:
        with self._seq_lock:
            n = self._seq_cache.get(session_id, 0) + 1
            self._seq_cache[session_id] = n
            return n

    def _persist_seq(self) -> None:
        if self._store:
            self._store._set_meta("seq_cache", dict(self._seq_cache))  # type: ignore[attr-defined]

    # -- capture -------------------------------------------------------------

    def on_turn_recorded(self, turn: Dict[str, Any]) -> None:
        """Capture a completed turn (user, assistant, CoT, tool calls) as a graph."""
        if not self._available or not self._cfg:
            return
        cap = self._cfg.get("capture") or {}
        messages = turn.get("messages") or []
        session_id = turn.get("session_id") or self._session_id
        if not session_id:
            return

        # Locate this turn's tail: from the last user message onward.
        start = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                start = i
                break
        segment = messages[start:]

        nodes: List[Dict[str, Any]] = []
        rels: List[Dict[str, Any]] = []
        prev_msg_id: Optional[str] = None

        for msg in segment:
            role = msg.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            seq = self._next_seq(session_id)
            if role == "user":
                if not cap.get("user_messages", True):
                    continue
                content = _msg_text(msg)
                nid = f"msg:{session_id}:{seq}:{_hash8(content)}"
                nodes.append(_node(nid, "message", {"role": "user", "content": content,
                                                   "seq": seq, "session_id": session_id}))
                if prev_msg_id:
                    rels.append(_rel(prev_msg_id, "FOLLOWED_BY", nid))
                prev_msg_id = nid
            elif role == "assistant":
                content = _msg_text(msg)
                reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
                nid = f"msg:{session_id}:{seq}:{_hash8(content)}"
                if cap.get("assistant_messages", True) and content:
                    nodes.append(_node(nid, "message", {"role": "assistant", "content": content,
                                                       "seq": seq, "session_id": session_id}))
                    if prev_msg_id:
                        rels.append(_rel(prev_msg_id, "FOLLOWED_BY", nid))
                    prev_msg_id = nid
                # Chain-of-thought capture.
                if cap.get("chain_of_thought", True) and reasoning and reasoning.strip():
                    rid = f"reasoning:{session_id}:{seq}:{_hash8(reasoning)}"
                    nodes.append(_node(rid, "reasoning", {"content": reasoning,
                                                         "seq": seq, "session_id": session_id}))
                    if content:
                        rels.append(_rel(nid, "REASONED", rid))
                # Tool calls.
                if cap.get("tool_calls", True):
                    for tc in (msg.get("tool_calls") or []):
                        fn = tc.get("function") or {}
                        name = fn.get("name", "")
                        args = fn.get("arguments", "")
                        tcid = tc.get("id") or f"{seq}:{name}"
                        tid = f"toolcall:{session_id}:{seq}:{_hash8(name + str(args))}"
                        nodes.append(_node(tid, "toolcall",
                                          {"name": name, "arguments": str(args),
                                           "content": f"{name}({args})",
                                           "seq": seq, "session_id": session_id}))
                        if content:
                            rels.append(_rel(nid, "CALLED", tid))
                        # Link to the matching tool result message.
                        for tm in segment:
                            if tm.get("role") == "tool" and tm.get("tool_call_id") == tcid:
                                rc = _msg_text(tm)
                                rnid = f"toolresult:{session_id}:{seq}:{_hash8(rc)}:{_hash8(tcid)}"
                                nodes.append(_node(rnid, "message",
                                                  {"role": "tool_result", "content": rc,
                                                   "seq": seq, "session_id": session_id}))
                                rels.append(_rel(tid, "PRODUCED", rnid))
                                break
            elif role == "tool":
                # Standalone tool result (no assistant tool_calls in segment) — capture lightly.
                if not cap.get("tool_calls", True):
                    continue
                rc = _msg_text(msg)
                if not rc:
                    continue
                nodes.append(_node(f"toolresult:{session_id}:{seq}:{_hash8(rc)}", "message",
                                  {"role": "tool_result", "content": rc,
                                   "seq": seq, "session_id": session_id}))

        # Attach top-level nodes to the session.
        for n in nodes:
            if n["kind"] in ("message", "reasoning", "toolcall"):
                rels.append(_rel(f"sess:{session_id}", "HAS", n["id"]))

        if nodes or rels:
            self._enqueue_job(session_id, {
                "type": "turn", "session_id": session_id, "nodes": nodes, "rels": rels,
            })

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if not self._available or not self._session_id:
            return
        # Mark ended. Do not re-capture the full tail here: live turn capture
        # uses sequence-derived ids, so replaying all messages at shutdown can
        # duplicate nodes. Historical repair uses session_backfill.py, which
        # derives stable ids from SQLite message rows and is safe to re-run.
        self._enqueue_job(self._session_id, {
            "type": "finalize", "session_id": self._session_id,
        })
        self._persist_seq()

    # sync_turn is intentionally delegated to on_turn_recorded (richer payload).
    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        return

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """Capture a delegation event (this session → a sub-agent) as a graph.

        The delegation is modelled as a ``delegation`` node attached to the
        parent session, with a ``DELEGATED_TO`` edge to the child session node
        (created lazily) and a ``PRODUCED`` edge to the child's result message.
        """
        if not self._available or not self._session_id:
            return
        goal = kwargs.get("goal") or task or ""
        context = kwargs.get("context") or ""
        if child_session_id:
            child_sess_node = f"sess:{child_session_id}"
        else:
            child_sess_node = f"delegated:{_hash8(goal)}"
        did = f"deleg:{self._session_id}:{_hash8(goal + (child_session_id or ''))}"
        nodes: List[Dict[str, Any]] = [
            _node(did, "delegation", {
                "goal": goal, "context": context, "result": result or "",
                "child_session_id": child_session_id or "",
                "session_id": self._session_id,
            }),
            # Lazily ensure the child session node exists (opencode-cli children
            # never run through session_start → no other code creates it).
            _node(child_sess_node, "session", {
                "session_id": child_session_id or did,
                "title": f"delegated: {goal[:80]}",
                "source": "delegation",
            }),
        ]
        rels: List[Dict[str, Any]] = [
            _rel(f"sess:{self._session_id}", "DELEGATED_TO", did),
            _rel(did, "DELEGATED_TO", child_sess_node),
        ]
        if result:
            rid = f"msg:{did}:0:{_hash8(result)}"
            nodes.append(_node(rid, "message", {
                "role": "assistant", "content": result,
                "seq": 0, "session_id": child_session_id or did,
            }))
            rels.append(_rel(child_sess_node, "HAS", rid))
            rels.append(_rel(did, "PRODUCED", rid))
        self._enqueue_job(self._session_id, {
            "type": "turn", "session_id": self._session_id,
            "nodes": nodes, "rels": rels,
        })

    # -- prefetch / recall ---------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._available or not self._embed or not query:
            return ""
        if not self._prefetch_result:
            return ""
        out = self._prefetch_result
        self._prefetch_result = ""
        return out

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._available or not self._embed or not query:
            return
        try:
            cap = self._cfg.get("search", {})
            tier = self._tier()
            ts = self._tier_settings(tier)
            scope = cap.get("scope", "both")
            dw = float(cap.get("doc_weight") or 0.6)
            cw = float(cap.get("cot_weight") or 0.4)
            hits = self._weighted_recall(
                query,
                top_k=int(ts.get("prefetch_top_k") or int(cap.get("top_k") or 8)),
                scope=scope, doc_w=dw, cot_w=cw,
                cutoff=float(cap.get("similarity_cutoff") or 0.0),
            )
            if not hits:
                return
            max_chars = int(ts.get("max_recall_chars") or 600)
            inject_full = bool(ts.get("inject_full_text"))
            lines = ["[Knowledge Graph recall]"]
            doc_paths: List[str] = []
            for h in hits:
                kind = h.get("kind", "?")
                score = h.get("weighted_score", h.get("score", 0.0))
                content = (h.get("content") or "").replace("\n", " ")
                # Always record the real source path (used by the read-source nudge).
                ptr = h.get("path") or ""
                if ptr:
                    doc_paths.append(ptr)
                if kind in ("docchunk", "doc") and not inject_full:
                    # Pointer, not the body — force the model to read the source.
                    head = h.get("heading") or ""
                    line = f"- (doc {score:.2f}) {ptr}" + (f" — {head}" if head else "")
                    lines.append(line[:200])
                else:
                    lines.append(f"- ({kind} {score:.2f}) {content[:max_chars]}")
            if doc_paths and ts.get("read_source_nudge"):
                uniq = []
                for p in doc_paths:
                    if p and p not in uniq:
                        uniq.append(p)
                lines.append(
                    "READ THESE SOURCE FILES before answering (use read_file, not the "
                    "graph summary): " + ", ".join(uniq[:10])
                )
            with self._prefetch_lock:
                self._prefetch_result = "\n".join(lines)
        except Exception as exc:
            logger.debug("kg prefetch failed: %s", exc)

    # -- weighted mix of doc + chain-of-thought recall ------------------

    _DOC_KINDS = frozenset({"docchunk", "doc"})
    _CTX_KINDS = frozenset({"reasoning", "message", "idea"})

    def _weighted_recall(
        self, query: str, *, top_k: int = 8, scope: str = "both",
        doc_w: float = 0.6, cot_w: float = 0.4, cutoff: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Blend doc + chain-of-thought hits into one re-ranked list.

        scope: "both" | "docs" | "cot".  doc_w / cot_w blend the
        two score streams; if omitted and scope="both", default 60% doc
        / 40% CoT.  Returns top_k hits, each carrying its original
        ``score`` and a blended ``weighted_score``.
        """
        if not self._embed:
            return []
        vec = self._embed.embed(query)
        if not vec:
            return []
        kinds: List[str] = []
        if scope in ("both", "docs"):
            kinds += list(self._DOC_KINDS)
        if scope in ("both", "cot"):
            kinds += list(self._CTX_KINDS)
        if not kinds:
            return []
        pool = max(int(top_k) * 2, 24)
        hits = self._store.vector_search(vec, top_k=pool, kinds=kinds, cutoff=cutoff)
        if not hits:
            return []
        if scope == "docs":
            dw, cw = 1.0, 0.0
        elif scope == "cot":
            dw, cw = 0.0, 1.0
        else:
            tot = (doc_w + cot_w) or 1.0
            dw, cw = doc_w / tot, cot_w / tot
        out: List[Dict[str, Any]] = []
        for h in hits:
            kind = h.get("kind", "?")
            w = dw if kind in self._DOC_KINDS else (cw if kind in self._CTX_KINDS else 0.5)
            hh = dict(h)
            hh["weighted_score"] = round(float(h.get("score", 0.0)) * w, 4)
            out.append(hh)
        out.sort(key=lambda x: x["weighted_score"], reverse=True)
        return out[:int(top_k)]

    def system_prompt_block(self) -> str:
        tier = self._tier()
        lines = [
            "# Knowledge Graph (Second Brain)",
            "A persistent Neo4j knowledge graph records every session, including "
            "chain-of-thought reasoning and tool calls, as embeddable, linkable "
            "nodes. It is ALSO the agent's DOC SURFACE: markdown docs are "
            "indexed with their real file paths so recall points at the SOURCE, "
            "not a summary.",
            "Use kg_search for semantic + graph recall, kg_related to trace "
            "connections, kg_index_docs to index .md files, kg_read_doc to "
            "resolve a doc to its real file, and kg_remember_idea to capture "
            "ideas linking to docs/repos/file paths.",
        ]
        if tier == "weak":
            lines.append(
                "IMPORTANT: when a recall result references a doc (it shows a "
                "file path), you MUST open that file with read_file and read the "
                "FULL source before answering. Never answer from the graph "
                "summary alone — the graph is an index, not the document."
            )
        return "\n".join(lines)

    # -- per-model harness -------------------------------------------------

    def _tier(self) -> str:
        """Resolve the harness tier for the active model."""
        h = self._cfg.get("harness") or {}
        forced = (h.get("tier") or "").strip().lower()
        if forced in ("weak", "standard", "strong"):
            return forced
        model = (self._model or "").lower()
        if any(k in model for k in ("mini", "nano", "haiku", "flash-lite",
                                       "flash-preview", "small", "lite", "light",
                                       "nano", "-mini", ":mini")):
            return "weak"
        if any(k in model for k in ("opus", "large", "pro", "max", "-pro",
                                       ":pro", "sonnet", "gpt-5", "o3", "o4",
                                       "1.5-pro", "2.5-pro", "3-pro")):
            return "strong"
        return (h.get("default_tier") or "standard").strip().lower() or "standard"

    def _tier_settings(self, tier: str) -> Dict[str, Any]:
        h = self._cfg.get("harness") or {}
        tiers = h.get("tiers") or {}
        return tiers.get(tier) or {
            "inject_full_text": False, "read_source_nudge": False,
            "prefetch_top_k": 8, "max_recall_chars": 600,
        }

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [KG_SEARCH_SCHEMA, KG_QUERY_SCHEMA, KG_RELATED_SCHEMA,
                KG_REMEMBER_IDEA_SCHEMA, KG_READ_DOC_SCHEMA,
                KG_INDEX_DOCS_SCHEMA, KG_FORGET_SCHEMA, KG_IMPORT_SESSIONS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._available or not self._store:
            return tool_error("Knowledge graph is not connected")
        try:
            return json.dumps(self._dispatch(tool_name, args), ensure_ascii=False)
        except Exception as exc:
            return tool_error(str(exc))

    def _dispatch(self, tool_name: str, args: Dict[str, Any]) -> Any:
        if tool_name == "kg_search":
            return self._tool_search(args)
        if tool_name == "kg_query":
            return self._tool_query(args)
        if tool_name == "kg_related":
            return self._tool_related(args)
        if tool_name == "kg_remember_idea":
            return self._tool_remember_idea(args)
        if tool_name == "kg_read_doc":
            return self._tool_read_doc(args)
        if tool_name == "kg_index_docs":
            return self._tool_index_docs(args)
        if tool_name == "kg_forget":
            return self._tool_forget(args)
        if tool_name == "kg_import_sessions":
            return self._tool_import_sessions(args)
        return {"error": f"Unknown tool: {tool_name}"}

    def _tool_search(self, args: Dict[str, Any]) -> Any:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        if not self._embed:
            return {"error": "embeddings unavailable — cannot run semantic search"}
        top_k = min(int(args.get("top_k") or 8), 30)
        cap = self._cfg.get("search", {})
        scope = args.get("scope") or cap.get("scope", "both")
        if scope not in ("both", "docs", "cot"):
            scope = "both"
        doc_w = float(args["doc_weight"]) if args.get("doc_weight") is not None else float(cap.get("doc_weight") or 0.6)
        cot_w = float(args["cot_weight"]) if args.get("cot_weight") is not None else float(cap.get("cot_weight") or 0.4)
        cutoff = float(cap.get("similarity_cutoff") or 0.0)
        hits = self._weighted_recall(query, top_k=top_k, scope=scope,
                                     doc_w=doc_w, cot_w=cot_w, cutoff=cutoff)
        if args.get("expand_graph"):
            for h in hits:
                try:
                    h["neighbors"] = self._store.neighbors(h["id"], limit=10)
                except Exception:
                    h["neighbors"] = []
        return {"results": hits, "scope": scope,
                "weights": {"doc": doc_w, "cot": cot_w}}

    def _tool_query(self, args: Dict[str, Any]) -> Any:
        cypher = (args.get("cypher") or "").strip()
        if not cypher:
            return {"error": "cypher is required"}
        limit = int(args.get("limit") or 50)
        rows = self._store.run_cypher(cypher[:8000])
        return {"rows": rows[:limit], "count": len(rows[:limit])}

    def _tool_related(self, args: Dict[str, Any]) -> Any:
        node_id = args.get("node_id") or ""
        text = (args.get("text") or "").strip()
        rel_types = args.get("rel_types") or None
        limit = int(args.get("limit") or 30)
        if not node_id and text and self._embed:
            vec = self._embed.embed(text)
            hits = self._store.vector_search(vec, top_k=1) if vec else []
            if not hits:
                return {"error": f"No node found near: {text}"}
            node_id = hits[0]["id"]
        if not node_id:
            return {"error": "node_id or text is required"}
        return {"node_id": node_id, "neighbors": self._store.neighbors(node_id, rel_types, limit)}

    def _tool_remember_idea(self, args: Dict[str, Any]) -> Any:
        content = (args.get("content") or "").strip()
        if not content:
            return {"error": "content is required"}
        session_id = self._session_id or "global"
        seq = self._next_seq(session_id)
        idea_id = f"idea:{session_id}:{seq}:{_hash8(content)}"
        tags = args.get("tags") or []
        nodes = [_node(idea_id, "idea", {"content": content, "tags": json.dumps(tags),
                                          "seq": seq, "session_id": session_id,
                                          "created_at": _now_iso()})]
        rels: List[Dict[str, Any]] = [_rel(f"sess:{session_id}", "HAS", idea_id)]
        # Link to entities (deduped by kind+value).
        for link in (args.get("links") or []):
            kind = (link.get("kind") or "concept")
            value = (link.get("value") or "").strip()
            if not value:
                continue
            ent_id = f"entity:{kind}:{_hash8(value)}"
            nodes.append(_node(ent_id, "entity", {"kind": kind, "value": value,
                                               "name": value, "session_id": session_id}))
            rels.append(_rel(idea_id, "LINKS_TO", ent_id,
                              {"note": link.get("note") or ""}))
            # Entity also hangs off the session for traversal.
            rels.append(_rel(f"sess:{session_id}", "HAS", ent_id))
        self._enqueue_job(session_id, {"type": "idea", "session_id": session_id,
                                       "nodes": nodes, "rels": rels})
        return {"idea_id": idea_id, "linked": len([n for n in nodes if n["kind"] == "entity"])}

    def _tool_forget(self, args: Dict[str, Any]) -> Any:
        node_id = (args.get("node_id") or "").strip()
        if not node_id:
            return {"error": "node_id is required"}
        try:
            self._store.run_cypher(
                "MATCH (n {id: $id}) DETACH DELETE n", {"id": node_id}
            )
            return {"deleted": node_id}
        except Exception as exc:
            return {"error": str(exc)}

    # -- doc surface --------------------------------------------------------

    def _tool_index_docs(self, args: Dict[str, Any]) -> Any:
        from pathlib import Path as _P

        paths = args.get("paths") or []
        if not paths:
            return {"error": "paths is required"}
        recursive = bool(args.get("recursive", True))
        glob = args.get("glob") or "*.md"
        files: List[_P] = []
        for p in paths:
            pp = _P(os.path.expanduser(str(p))).resolve()
            if pp.is_dir():
                files.extend(sorted(pp.rglob(glob) if recursive else pp.glob(glob)))
            elif pp.exists():
                files.append(pp)
        if not files:
            return {"error": f"No files matched under {paths}"}
        session_id = self._session_id or "global"
        total_chunks = 0
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.warning("kg index_docs skip %s: %s", f, exc)
                continue
            chunks = _chunk_markdown(text, max_chars=1500, overlap=200)
            if not chunks:
                continue
            doc_id = f"entity:doc:{_hash8(str(f))}"
            seq = self._next_seq(session_id)
            nodes: List[Dict[str, Any]] = [_node(
                doc_id, "doc",
                {"kind": "doc", "value": str(f), "path": str(f),
                 "name": f.name, "mtime": f.stat().st_mtime,
                 "char_count": len(text), "chunk_count": len(chunks),
                 "session_id": session_id},
                labels=["doc"],
            )]
            rels: List[Dict[str, Any]] = [
                _rel(f"sess:{session_id}", "HAS", doc_id),
            ]
            for idx, (heading, body) in enumerate(chunks):
                cid = f"chunk:{_hash8(str(f))}:{idx}"
                nodes.append(_node(
                    cid, "docchunk",
                    {"path": str(f), "heading": heading, "chunk_index": idx,
                     "content": (f"# {heading}\n" if heading else "") + body,
                     "session_id": session_id},
                ))
                rels.append(_rel(doc_id, "HAS_CHUNK", cid))
                rels.append(_rel(f"sess:{session_id}", "HAS", cid))
                total_chunks += 1
            self._enqueue_job(session_id, {"type": "doc", "session_id": session_id,
                                       "nodes": nodes, "rels": rels})
        return {"indexed_files": len(files), "chunks": total_chunks}

    def _tool_read_doc(self, args: Dict[str, Any]) -> Any:
        from pathlib import Path as _P

        path = (args.get("path") or "").strip()
        doc_id = (args.get("doc_id") or "").strip()
        if not path and not doc_id:
            return {"error": "path or doc_id is required"}
        if not path and doc_id:
            # Resolve doc node -> path.
            try:
                rows = self._store.run_cypher(
                    "MATCH (d {id: $id}) RETURN d.path AS p, d.name AS n",
                    {"id": doc_id},
                )
                if rows:
                    path = rows[0].get("p") or ""
            except Exception:
                pass
        if not path:
            return {"error": "could not resolve doc to a file path"}
        p = _P(os.path.expanduser(path))
        if not p.exists():
            return {"error": f"file not found: {path}"}
        snippet_chars = int(args.get("snippet_chars") or 600)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"error": str(exc)}
        return {
            "path": str(p),
            "name": p.name,
            "note": "OPEN THE FULL SOURCE with read_file before answering — "
                    "the graph only summarizes docs.",
            "char_count": len(text),
            "snippet": text[:snippet_chars],
        }

    def _tool_import_sessions(self, args: Dict[str, Any]) -> Any:
        from hermes_constants import get_hermes_home
        from .session_backfill import import_state_db

        db_path = args.get("db_path") or str(get_hermes_home() / "state.db")
        dry_run = bool(args.get("dry_run", True))
        progress: List[str] = []
        return import_state_db(
            self._store,
            self._embed,
            str(db_path),
            since_ts=args.get("since_ts"),
            limit_sessions=args.get("limit_sessions"),
            session_ids=args.get("session_ids") or None,
            embed_tools=bool(args.get("embed_tools", False)),
            dry_run=dry_run,
            progress=progress.append,
        ) | {"progress": progress, "db_path": str(db_path)}

    # -- job application (background worker) -----------------------------------

    def _apply_job(self, job: Dict[str, Any]) -> None:
        if not self._store:
            return
        jtype = job.get("type")

        if jtype == "session":
            sid = job.get("session_id")
            props = dict(job.get("props") or {})
            props.update({
                "session_id": sid,
                "platform": job.get("platform", ""),
                "profile": job.get("profile", ""),
                "model": job.get("model", ""),
                "parent_session_id": job.get("parent_session_id", ""),
                "reset": bool(job.get("reset", False)),
                "last_event": job.get("event", "start"),
            })
            props.setdefault("created_at", _now_iso())
            self._store.merge_node(
                f"sess:{sid}", "session",
                labels=["KgSession"],
                props=props,
            )
            parent = job.get("parent_session_id") or ""
            if parent and parent != sid:
                rel = "RESET_TO" if job.get("reset") else "DERIVED_TO"
                self._store.merge_node(
                    f"sess:{parent}", "session", labels=["KgSession"],
                    props={"session_id": parent},
                )
                self._store.merge_relationship(f"sess:{parent}", rel, f"sess:{sid}")
            self._store.ensure_schema(self._embed.dimension if self._embed else None)
            return

        if jtype == "finalize":
            sid = job.get("session_id")
            self._store.merge_node(
                f"sess:{sid}", "session", labels=["KgSession"],
                props={"session_id": sid, "ended_at": _now_iso()},
            )
            return

        # turn / idea / doc: embed node texts, write nodes, then relationships.
        # Doc metadata nodes (kind="doc") carry no embeddable body — only
        # their DocChunk children get vectors.
        nodes = job.get("nodes") or []
        rels = job.get("rels") or []
        if self._embed and nodes:
            texts = [_node_text(n) if n["kind"] != "doc" else "" for n in nodes]
            vecs = self._embed.embed_many(texts)
            for n, vec in zip(nodes, vecs):
                self._store.merge_node(
                    n["id"], n["kind"], n.get("labels", []), n["props"], vec,
                )
        else:
            for n in nodes:
                self._store.merge_node(n["id"], n["kind"], n.get("labels", []), n["props"])
        for r in rels:
            self._store.merge_relationship(r["src"], r["rel"], r["dst"], r.get("props"))
        # Make sure the vector index exists once we know the dimension.
        if self._embed and self._embed.dimension:
            self._store.ensure_schema(self._embed.dimension)

    # -- shutdown -------------------------------------------------------------

    def shutdown(self) -> None:
        try:
            self._persist_seq()
        except Exception:
            pass
        if self._queue:
            self._queue.shutdown()
        if self._store:
            self._store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif "text" in part:
                    parts.append(str(part.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _node_text(n: Dict[str, Any]) -> str:
    props = n.get("props") or {}
    return (props.get("content") or props.get("value") or props.get("name")
            or props.get("arguments") or "")


def _node(node_id: str, kind: str, props: Dict[str, Any],
          labels: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"id": node_id, "kind": kind, "props": props, "labels": labels or []}


def _rel(src: str, rel: str, dst: str, props: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"src": src, "rel": rel, "dst": dst, "props": props or {}}


def _hash8(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:8]


def _chunk_markdown(text: str, max_chars: int = 1500, overlap: int = 200) -> List[tuple]:
    """Split markdown into heading-bounded chunks.

    Each chunk = a heading (or "Intro") plus its body. Long bodies are
    further windowed with overlap so semantic search hits stay self-contained.
    Returns list of (heading, body) tuples.
    """
    import re

    if not text or not text.strip():
        return []
    lines = text.splitlines()
    # Group by ATX headings.
    sections: List[tuple] = []
    cur_head = "Intro"
    cur: List[str] = []
    head_re = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
    for line in lines:
        m = head_re.match(line)
        if m:
            if cur:
                sections.append((cur_head, "\n".join(cur).strip()))
            cur_head = m.group(2).strip()
            cur = []
        else:
            cur.append(line)
    if cur:
        sections.append((cur_head, "\n".join(cur).strip()))

    chunks: List[tuple] = []
    for head, body in sections:
        if not body:
            continue
        if len(body) <= max_chars:
            chunks.append((head, body))
            continue
        # Window the long body, preserving heading context in chunk 1.
        start = 0
        first = True
        while start < len(body):
            end = min(start + max_chars, len(body))
            piece = body[start:end].strip()
            if piece:
                chunks.append((head if first else f"{head} (cont.)", piece))
            first = False
            if end >= len(body):
                break
            start = max(end - overlap, start + 1)
    return chunks or [("Intro", text.strip()[:max_chars])]
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the knowledge-graph memory provider."""
    ctx.register_memory_provider(KnowledgeGraphMemoryProvider())


