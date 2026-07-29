"""Neo4j knowledge-graph memory provider for Hermes Agent.

The plugin uses only the public MemoryProvider lifecycle. Completed turns arrive
through ``sync_turn(..., messages=...)`` and are written by a durable background
queue, so Neo4j and embedding latency never block the conversation path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agent.memory_provider import MemoryProvider

from .queue_store import DurableQueue as _DurableQueue

logger = logging.getLogger(__name__)

_ALLOWED_RELATIONSHIPS = {
    "HAS",
    "FOLLOWED_BY",
    "CALLED",
    "PRODUCED",
    "REASONED",
    "DELEGATED_TO",
    "LINKS_TO",
}
_WRITE_CYPHER = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH|GRANT|DENY|REVOKE)\b",
    re.IGNORECASE,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {
                "text",
                "input_text",
                "output_text",
            }:
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _json_result(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


class KnowledgeGraphMemoryProvider(MemoryProvider):
    """Persistent session, delegation, idea, and document graph in Neo4j."""

    def __init__(self) -> None:
        self._cfg: Dict[str, Any] = {}
        self._driver = None
        self._queue: Optional[_DurableQueue] = None
        self._hermes_home = Path.home() / ".hermes"
        self._session_id = ""
        self._platform = "cli"
        self._model = ""
        self._available = False
        self._prefetch: Dict[str, str] = {}
        self._prefetch_lock = threading.Lock()
        self._vector_dimensions: Optional[int] = None

    @property
    def name(self) -> str:
        return "knowledge_graph"

    def _config_path(self, hermes_home: Optional[Path] = None) -> Path:
        return Path(hermes_home or self._hermes_home) / "knowledge_graph.json"

    def _load_config(self) -> Dict[str, Any]:
        defaults: Dict[str, Any] = {
            "enabled": False,
            "uri": "bolt://localhost:7687",
            "user": "neo4j",
            "password": "",
            "database": "neo4j",
            "capture_reasoning": False,
            "capture_tool_arguments": False,
            "capture_tool_results": False,
            "prefetch_top_k": 6,
            "prefetch_max_chars": 1800,
            "embeddings_base_urls": [],
            "embeddings_model": "",
            "embeddings_api_key": "",
            "embedding_timeout": 15.0,
        }
        section: Dict[str, Any] = {}
        try:
            from hermes_cli.config import cfg_get, load_config

            section = cfg_get(
                load_config(),
                "knowledge_graph",
                default={},
            ) or {}
        except Exception:
            section = {}
        try:
            from hermes_constants import get_hermes_home

            path = self._config_path(get_hermes_home())
            if path.exists():
                section = {
                    **section,
                    **json.loads(path.read_text(encoding="utf-8")),
                }
        except Exception:
            pass

        cfg = {**defaults, **section}
        cfg["enabled"] = bool(
            cfg.get("enabled")
            or os.getenv("HERMES_KG_ENABLED", "").lower()
            in {"1", "true", "yes"}
        )
        cfg["uri"] = os.getenv("NEO4J_URI", "") or str(
            cfg.get("uri") or defaults["uri"]
        )
        cfg["user"] = os.getenv("NEO4J_USER", "") or str(
            cfg.get("user") or defaults["user"]
        )
        cfg["password"] = os.getenv("NEO4J_PASSWORD", "") or str(
            cfg.get("password") or ""
        )
        cfg["database"] = os.getenv("NEO4J_DATABASE", "") or str(
            cfg.get("database") or defaults["database"]
        )
        env_urls = os.getenv("HERMES_KG_EMBEDDINGS_URLS", "")
        if env_urls:
            cfg["embeddings_base_urls"] = [
                url.strip()
                for url in env_urls.split(",")
                if url.strip()
            ]
        elif isinstance(
            cfg.get("embeddings_base_url"),
            str,
        ) and cfg.get("embeddings_base_url"):
            cfg["embeddings_base_urls"] = [
                cfg["embeddings_base_url"]
            ]
        cfg["embeddings_model"] = os.getenv(
            "HERMES_KG_EMBEDDINGS_MODEL",
            "",
        ) or str(cfg.get("embeddings_model") or "")
        cfg["embeddings_api_key"] = os.getenv(
            "HERMES_KG_EMBEDDINGS_API_KEY",
            "",
        ) or str(cfg.get("embeddings_api_key") or "")
        return cfg

    def is_available(self) -> bool:
        self._cfg = self._load_config()
        return bool(
            self._cfg.get("enabled")
            and self._cfg.get("uri")
            and importlib.util.find_spec("neo4j") is not None
        )

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "enabled",
                "description": "Enable the Neo4j knowledge graph provider",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "uri",
                "description": (
                    "Neo4j URI, for example bolt://localhost:7687"
                ),
                "default": "bolt://localhost:7687",
            },
            {
                "key": "user",
                "description": "Neo4j username",
                "default": "neo4j",
            },
            {
                "key": "password",
                "description": "Neo4j password",
                "secret": True,
                "env_var": "NEO4J_PASSWORD",
            },
            {
                "key": "database",
                "description": "Neo4j database",
                "default": "neo4j",
            },
            {
                "key": "embeddings_base_urls",
                "description": (
                    "Comma-separated OpenAI-compatible embedding endpoints"
                ),
                "default": "",
            },
            {
                "key": "embeddings_model",
                "description": (
                    "Embedding model exposed by the local endpoint"
                ),
                "default": "",
            },
            {
                "key": "capture_reasoning",
                "description": (
                    "Store model reasoning fields (privacy-sensitive)"
                ),
                "default": "false",
                "choices": ["true", "false"],
            },
            {
                "key": "capture_tool_arguments",
                "description": (
                    "Store raw tool arguments (privacy-sensitive)"
                ),
                "default": "false",
                "choices": ["true", "false"],
            },
            {
                "key": "capture_tool_results",
                "description": (
                    "Store raw tool results (privacy-sensitive)"
                ),
                "default": "false",
                "choices": ["true", "false"],
            },
        ]

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def save_config(
        self,
        values: Dict[str, Any],
        hermes_home: str,
    ) -> None:
        cleaned = dict(values)
        urls = cleaned.get("embeddings_base_urls")
        if isinstance(urls, str):
            cleaned["embeddings_base_urls"] = [
                url.strip()
                for url in urls.split(",")
                if url.strip()
            ]
        for key in (
            "enabled",
            "capture_reasoning",
            "capture_tool_arguments",
            "capture_tool_results",
        ):
            if key in cleaned:
                cleaned[key] = self._as_bool(cleaned[key])
        path = self._config_path(Path(hermes_home))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(cleaned, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def initialize(self, session_id: str, **kwargs) -> None:
        self._cfg = self._load_config()
        self._session_id = session_id or ""
        self._hermes_home = Path(
            str(kwargs.get("hermes_home") or self._hermes_home)
        )
        self._platform = str(kwargs.get("platform") or "cli")
        self._model = str(kwargs.get("model") or "")
        if not self._cfg.get("enabled"):
            return
        try:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self._cfg["uri"],
                auth=(
                    self._cfg["user"],
                    self._cfg["password"],
                ),
            )
            self._driver.verify_connectivity()
            self._ensure_schema()
        except Exception as exc:
            logger.warning(
                "Knowledge graph disabled: Neo4j connection failed: %s",
                exc,
            )
            self._driver = None
            self._available = False
            return
        self._available = True
        queue_path = (
            self._hermes_home
            / "knowledge_graph"
            / "pending.db"
        )
        self._queue = _DurableQueue(queue_path, self._apply_job)
        if self._session_id:
            self._enqueue_session(self._session_id, event="start")

    def _session(self):
        if not self._driver:
            raise RuntimeError("Neo4j is not connected")
        return self._driver.session(
            database=self._cfg.get("database") or "neo4j"
        )

    def _ensure_schema(self) -> None:
        statements = [
            (
                "CREATE CONSTRAINT kg_node_id IF NOT EXISTS "
                "FOR (n:KgNode) REQUIRE n.id IS UNIQUE"
            ),
            (
                "CREATE FULLTEXT INDEX kg_text IF NOT EXISTS "
                "FOR (n:KgNode) "
                "ON EACH [n.content, n.title, n.path, n.tags]"
            ),
        ]
        with self._session() as session:
            for statement in statements:
                try:
                    session.run(statement).consume()
                except Exception as exc:
                    logger.debug(
                        "Knowledge graph schema statement skipped: %s",
                        exc,
                    )

    def _ensure_vector_index(self, dimensions: int) -> None:
        if self._vector_dimensions == dimensions:
            return
        statement = (
            "CREATE VECTOR INDEX kg_embedding IF NOT EXISTS "
            "FOR (n:KgNode) ON (n.embedding) "
            f"OPTIONS {{indexConfig: {{`vector.dimensions`: "
            f"{int(dimensions)}, "
            "`vector.similarity_function`: 'cosine'}}"
        )
        try:
            with self._session() as session:
                session.run(statement).consume()
            self._vector_dimensions = dimensions
        except Exception as exc:
            logger.debug(
                "Neo4j vector index unavailable; "
                "full-text fallback remains active: %s",
                exc,
            )

    def _embed(self, text: str) -> Optional[List[float]]:
        urls = self._cfg.get("embeddings_base_urls") or []
        model = str(self._cfg.get("embeddings_model") or "")
        if not urls or not model or not text.strip():
            return None
        payload = json.dumps(
            {
                "model": model,
                "input": text[:12000],
            }
        ).encode("utf-8")
        for base in urls:
            endpoint = str(base).rstrip("/")
            if not endpoint.endswith("/embeddings"):
                endpoint += "/embeddings"
            headers = {"Content-Type": "application/json"}
            api_key = str(
                self._cfg.get("embeddings_api_key") or ""
            )
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            request = urllib.request.Request(
                endpoint,
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=float(
                        self._cfg.get("embedding_timeout")
                        or 15.0
                    ),
                ) as response:
                    body = json.loads(
                        response.read().decode("utf-8")
                    )
                vector = body["data"][0]["embedding"]
                if isinstance(vector, list) and vector:
                    result = [float(value) for value in vector]
                    self._ensure_vector_index(len(result))
                    return result
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                urllib.error.URLError,
            ) as exc:
                logger.debug(
                    "Embedding endpoint %s failed: %s",
                    endpoint,
                    exc,
                )
        return None

    def _enqueue(self, payload: Dict[str, Any]) -> None:
        if self._available and self._queue:
            self._queue.enqueue(payload)

    def _enqueue_session(
        self,
        session_id: str,
        *,
        parent_session_id: str = "",
        event: str = "start",
        ended: bool = False,
    ) -> None:
        props = {
            "session_id": session_id,
            "title": f"Hermes session {session_id}",
            "platform": self._platform,
            "model": self._model,
            "event": event,
            "updated_at": _utc_now(),
        }
        if ended:
            props["ended_at"] = _utc_now()
        nodes = [
            {
                "id": f"session:{session_id}",
                "kind": "session",
                "props": props,
            }
        ]
        relationships = []
        if parent_session_id:
            nodes.append(
                {
                    "id": f"session:{parent_session_id}",
                    "kind": "session",
                    "props": {
                        "session_id": parent_session_id,
                    },
                }
            )
            relationships.append(
                {
                    "from": f"session:{parent_session_id}",
                    "type": "DELEGATED_TO",
                    "to": f"session:{session_id}",
                }
            )
        self._enqueue(
            {
                "type": "upsert",
                "nodes": nodes,
                "relationships": relationships,
            }
        )

    def _nodes_from_messages(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        *,
        turn_token: str = "",
    ) -> tuple[
        List[Dict[str, Any]],
        List[Dict[str, str]],
    ]:
        token = turn_token or uuid.uuid4().hex
        start = 0
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "user":
                start = index
                break
        segment = messages[start:]
        nodes: List[Dict[str, Any]] = []
        relationships: List[Dict[str, str]] = []
        previous_id = ""
        tool_call_nodes: Dict[str, str] = {}

        for offset, message in enumerate(segment):
            role = str(message.get("role") or "")
            if role not in {"user", "assistant", "tool"}:
                continue
            content = _message_text(message)
            if role == "tool" and not self._as_bool(
                self._cfg.get("capture_tool_results")
            ):
                content = (
                    "[tool result omitted; "
                    "enable capture_tool_results to store]"
                )
            node_id = (
                f"message:{session_id}:{token}:{offset}:"
                f"{_hash(role + content)}"
            )
            props: Dict[str, Any] = {
                "session_id": session_id,
                "role": role,
                "content": content[:50000],
                "sequence": offset,
                "turn_token": token,
                "created_at": _utc_now(),
            }
            nodes.append(
                {
                    "id": node_id,
                    "kind": "message",
                    "props": props,
                }
            )
            relationships.append(
                {
                    "from": f"session:{session_id}",
                    "type": "HAS",
                    "to": node_id,
                }
            )
            if previous_id:
                relationships.append(
                    {
                        "from": previous_id,
                        "type": "FOLLOWED_BY",
                        "to": node_id,
                    }
                )
            previous_id = node_id

            if role == "assistant":
                reasoning = str(
                    message.get("reasoning")
                    or message.get("reasoning_content")
                    or ""
                )
                if self._as_bool(
                    self._cfg.get("capture_reasoning")
                ) and reasoning.strip():
                    reasoning_id = (
                        f"reasoning:{session_id}:{token}:{offset}:"
                        f"{_hash(reasoning)}"
                    )
                    nodes.append(
                        {
                            "id": reasoning_id,
                            "kind": "reasoning",
                            "props": {
                                "session_id": session_id,
                                "content": reasoning[:50000],
                                "turn_token": token,
                                "created_at": _utc_now(),
                            },
                        }
                    )
                    relationships.append(
                        {
                            "from": node_id,
                            "type": "REASONED",
                            "to": reasoning_id,
                        }
                    )
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    call_id = str(
                        call.get("id")
                        or _hash(
                            json.dumps(call, default=str)
                        )
                    )
                    tool_name = str(
                        function.get("name") or "unknown"
                    )
                    if self._as_bool(
                        self._cfg.get("capture_tool_arguments")
                    ):
                        arguments = str(
                            function.get("arguments") or ""
                        )
                    else:
                        arguments = (
                            "[redacted; enable "
                            "capture_tool_arguments to store]"
                        )
                    tool_node_id = (
                        f"tool:{session_id}:{token}:{offset}:"
                        f"{_hash(call_id)}"
                    )
                    tool_call_nodes[call_id] = tool_node_id
                    nodes.append(
                        {
                            "id": tool_node_id,
                            "kind": "tool_call",
                            "props": {
                                "session_id": session_id,
                                "name": tool_name,
                                "arguments": arguments[:50000],
                                "turn_token": token,
                                "created_at": _utc_now(),
                            },
                        }
                    )
                    relationships.append(
                        {
                            "from": node_id,
                            "type": "CALLED",
                            "to": tool_node_id,
                        }
                    )
            elif role == "tool":
                call_id = str(
                    message.get("tool_call_id") or ""
                )
                if call_id and call_id in tool_call_nodes:
                    relationships.append(
                        {
                            "from": tool_call_nodes[call_id],
                            "type": "PRODUCED",
                            "to": node_id,
                        }
                    )
        return nodes, relationships

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not self._available:
            return
        active_session = session_id or self._session_id
        if not active_session:
            return
        payload_messages = messages or [
            {
                "role": "user",
                "content": user_content,
            },
            {
                "role": "assistant",
                "content": assistant_content,
            },
        ]
        nodes, relationships = self._nodes_from_messages(
            payload_messages,
            active_session,
            turn_token=uuid.uuid4().hex,
        )
        if nodes:
            self._enqueue_session(active_session, event="turn")
            self._enqueue(
                {
                    "type": "upsert",
                    "nodes": nodes,
                    "relationships": relationships,
                }
            )

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        old_session_id = self._session_id
        self._session_id = new_session_id or self._session_id
        if self._available and new_session_id:
            self._enqueue_session(
                new_session_id,
                parent_session_id=(
                    parent_session_id
                    or (
                        old_session_id
                        if not reset
                        else ""
                    )
                ),
                event=str(
                    kwargs.get("reason")
                    or kwargs.get("event")
                    or "switch"
                ),
            )

    def on_session_end(
        self,
        messages: List[Dict[str, Any]],
    ) -> None:
        if self._available and self._session_id:
            self._enqueue_session(
                self._session_id,
                event="end",
                ended=True,
            )

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs,
    ) -> None:
        if not self._available or not self._session_id:
            return
        delegation_id = (
            f"delegation:{self._session_id}:"
            f"{_hash(task + child_session_id)}"
        )
        child_id = (
            child_session_id
            or f"external:{_hash(task)}"
        )
        nodes = [
            {
                "id": delegation_id,
                "kind": "delegation",
                "props": {
                    "session_id": self._session_id,
                    "task": task[:50000],
                    "result": result[:50000],
                    "created_at": _utc_now(),
                },
            },
            {
                "id": f"session:{child_id}",
                "kind": "session",
                "props": {
                    "session_id": child_id,
                    "title": f"Delegated: {task[:120]}",
                },
            },
        ]
        relationships = [
            {
                "from": f"session:{self._session_id}",
                "type": "DELEGATED_TO",
                "to": delegation_id,
            },
            {
                "from": delegation_id,
                "type": "DELEGATED_TO",
                "to": f"session:{child_id}",
            },
        ]
        self._enqueue(
            {
                "type": "upsert",
                "nodes": nodes,
                "relationships": relationships,
            }
        )

    def _apply_job(self, payload: Dict[str, Any]) -> None:
        if payload.get("type") != "upsert":
            raise ValueError(
                "Unsupported knowledge graph job: "
                f"{payload.get('type')}"
            )
        nodes = payload.get("nodes") or []
        relationships = payload.get("relationships") or []
        for node in nodes:
            props = node.get("props") or {}
            content = str(
                props.get("content")
                or props.get("result")
                or ""
            )
            if content and "embedding" not in props:
                embedding = self._embed(content)
                if embedding:
                    props["embedding"] = embedding
        with self._session() as session:
            if nodes:
                session.run(
                    """
                    UNWIND $nodes AS row
                    MERGE (n:KgNode {id: row.id})
                    SET n.kind = row.kind,
                        n += row.props,
                        n.updated_at = datetime()
                    """,
                    nodes=nodes,
                ).consume()
            grouped: Dict[
                str,
                List[Dict[str, str]],
            ] = {}
            for relationship in relationships:
                relationship_type = str(
                    relationship.get("type") or ""
                )
                if relationship_type in _ALLOWED_RELATIONSHIPS:
                    grouped.setdefault(
                        relationship_type,
                        [],
                    ).append(relationship)
            for relationship_type, rows in grouped.items():
                session.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (a:KgNode {{id: row.from}})
                    MATCH (b:KgNode {{id: row.to}})
                    MERGE (a)-[r:{relationship_type}]->(b)
                    SET r.updated_at = datetime()
                    """,
                    rows=rows,
                ).consume()

    def _fulltext_search(
        self,
        query: str,
        top_k: int,
    ) -> List[Dict[str, Any]]:
        try:
            with self._session() as session:
                records = session.run(
                    """
                    CALL db.index.fulltext.queryNodes(
                        'kg_text',
                        $query,
                        {limit: $limit}
                    )
                    YIELD node, score
                    RETURN node.id AS id,
                           node.kind AS kind,
                           node.content AS content,
                           node.title AS title,
                           node.path AS path,
                           score
                    ORDER BY score DESC
                    """,
                    query=query,
                    limit=top_k,
                )
                return [dict(record) for record in records]
        except Exception as exc:
            logger.debug(
                "Knowledge graph full-text search failed: %s",
                exc,
            )
            return self._substring_search(query, top_k)

    def _substring_search(
        self,
        query: str,
        top_k: int,
    ) -> List[Dict[str, Any]]:
        try:
            with self._session() as session:
                records = session.run(
                    """
                    MATCH (node:KgNode)
                    WHERE toLower(
                        coalesce(node.content, '')
                    ) CONTAINS toLower($query)
                       OR toLower(
                           coalesce(node.title, '')
                       ) CONTAINS toLower($query)
                       OR toLower(
                           coalesce(node.path, '')
                       ) CONTAINS toLower($query)
                    RETURN node.id AS id,
                           node.kind AS kind,
                           node.content AS content,
                           node.title AS title,
                           node.path AS path,
                           0.1 AS score
                    LIMIT $limit
                    """,
                    query=query,
                    limit=top_k,
                )
                return [dict(record) for record in records]
        except Exception as exc:
            logger.debug(
                "Knowledge graph substring search failed: %s",
                exc,
            )
            return []

    def _vector_search(
        self,
        embedding: List[float],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        try:
            with self._session() as session:
                records = session.run(
                    """
                    CALL db.index.vector.queryNodes(
                        'kg_embedding',
                        $limit,
                        $embedding
                    )
                    YIELD node, score
                    RETURN node.id AS id,
                           node.kind AS kind,
                           node.content AS content,
                           node.title AS title,
                           node.path AS path,
                           score
                    ORDER BY score DESC
                    """,
                    limit=top_k,
                    embedding=embedding,
                )
                return [dict(record) for record in records]
        except Exception as exc:
            logger.debug(
                "Knowledge graph vector search failed: %s",
                exc,
            )
            return []

    def _search(
        self,
        query: str,
        top_k: int,
    ) -> List[Dict[str, Any]]:
        top_k = max(1, min(int(top_k), 30))
        combined: Dict[str, Dict[str, Any]] = {}
        embedding = self._embed(query)
        sources = [
            (
                "vector",
                self._vector_search(
                    embedding,
                    top_k * 2,
                )
                if embedding
                else [],
            ),
            (
                "fulltext",
                self._fulltext_search(query, top_k * 2),
            ),
        ]
        for source, rows in sources:
            for row in rows:
                node_id = str(row.get("id") or "")
                if not node_id:
                    continue
                current = combined.setdefault(
                    node_id,
                    {**row, "sources": []},
                )
                current["score"] = max(
                    float(current.get("score") or 0),
                    float(row.get("score") or 0),
                )
                current["sources"].append(source)
        return sorted(
            combined.values(),
            key=lambda row: float(
                row.get("score") or 0
            ),
            reverse=True,
        )[:top_k]

    def queue_prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
    ) -> None:
        if not self._available or not query.strip():
            return
        key = session_id or self._session_id or "default"

        def worker() -> None:
            rows = self._search(
                query,
                int(self._cfg.get("prefetch_top_k") or 6),
            )
            if not rows:
                return
            lines = ["[Knowledge graph recall]"]
            for row in rows:
                text = str(
                    row.get("content")
                    or row.get("title")
                    or row.get("path")
                    or ""
                )
                if text:
                    lines.append(
                        "- "
                        f"{row.get('kind', 'node')}: "
                        f"{text[:500]}"
                    )
            value = "\n".join(lines)[
                : int(
                    self._cfg.get("prefetch_max_chars")
                    or 1800
                )
            ]
            with self._prefetch_lock:
                self._prefetch[key] = value

        threading.Thread(
            target=worker,
            name="hermes-kg-prefetch",
            daemon=True,
        ).start()

    def prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
    ) -> str:
        key = session_id or self._session_id or "default"
        with self._prefetch_lock:
            return self._prefetch.pop(key, "")

    def system_prompt_block(self) -> str:
        if not self._available:
            return ""
        return (
            "A Neo4j knowledge graph is active. Use kg_search "
            "before assuming prior sessions, delegated work, "
            "ideas, or indexed documents are unavailable. "
            "Reasoning, tool arguments, and tool results are "
            "not stored unless explicitly enabled."
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "kg_search",
                "description": (
                    "Hybrid vector and full-text search across "
                    "captured Hermes sessions and indexed documents."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {
                            "type": "integer",
                            "default": 8,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "kg_remember",
                "description": (
                    "Store a durable idea, decision, fact, or "
                    "insight in the knowledge graph."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "kg_index_docs",
                "description": (
                    "Index Markdown, text, or reStructuredText "
                    "files into the knowledge graph."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "recursive": {
                            "type": "boolean",
                            "default": True,
                        },
                    },
                    "required": ["paths"],
                },
            },
            {
                "name": "kg_query",
                "description": (
                    "Run a bounded read-only Cypher query for "
                    "graph traversal and aggregation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cypher": {"type": "string"},
                        "parameters": {"type": "object"},
                        "limit": {
                            "type": "integer",
                            "default": 50,
                        },
                    },
                    "required": ["cypher"],
                },
            },
            {
                "name": "kg_forget",
                "description": (
                    "Delete one knowledge-graph node by its "
                    "stable ID."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "node_id": {"type": "string"},
                    },
                    "required": ["node_id"],
                },
            },
            {
                "name": "kg_status",
                "description": (
                    "Show provider status and durable queue depth."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        ]

    def _tool_remember(
        self,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        content = str(args.get("content") or "").strip()
        if not content:
            raise ValueError("content is required")
        node_id = f"idea:{_hash(content)}"
        props: Dict[str, Any] = {
            "content": content[:50000],
            "tags": [
                str(tag)
                for tag in args.get("tags") or []
            ][:50],
            "created_at": _utc_now(),
            "session_id": self._session_id,
        }
        relationships = []
        if self._session_id:
            relationships.append(
                {
                    "from": f"session:{self._session_id}",
                    "type": "HAS",
                    "to": node_id,
                }
            )
        self._enqueue(
            {
                "type": "upsert",
                "nodes": [
                    {
                        "id": node_id,
                        "kind": "idea",
                        "props": props,
                    }
                ],
                "relationships": relationships,
            }
        )
        return {
            "ok": True,
            "node_id": node_id,
            "queued": True,
        }

    def _iter_documents(
        self,
        paths: Iterable[str],
        recursive: bool,
    ) -> Iterable[Path]:
        for raw in paths:
            path = Path(raw).expanduser().resolve()
            if path.is_file():
                yield path
            elif path.is_dir():
                iterator = (
                    path.rglob("*")
                    if recursive
                    else path.glob("*")
                )
                for candidate in iterator:
                    if candidate.is_file():
                        yield candidate

    def _chunks(
        self,
        text: str,
        size: int = 1800,
        overlap: int = 200,
    ) -> Iterable[str]:
        position = 0
        while position < len(text):
            end = min(len(text), position + size)
            chunk = text[position:end]
            if end < len(text):
                split = max(
                    chunk.rfind("\n\n"),
                    chunk.rfind("\n"),
                    chunk.rfind(" "),
                )
                if split > size // 2:
                    end = position + split
                    chunk = text[position:end]
            if chunk.strip():
                yield chunk.strip()
            if end >= len(text):
                break
            position = max(position + 1, end - overlap)

    def _tool_index_docs(
        self,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        paths = args.get("paths") or []
        recursive = bool(args.get("recursive", True))
        allowed = {
            ".md",
            ".markdown",
            ".txt",
            ".rst",
        }
        files = 0
        chunks = 0
        skipped = 0
        for path in self._iter_documents(paths, recursive):
            if path.suffix.lower() not in allowed:
                continue
            try:
                from agent.file_safety import get_read_block_error

                blocked = get_read_block_error(str(path))
            except Exception:
                blocked = None
            if blocked:
                skipped += 1
                continue
            try:
                text = path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                skipped += 1
                continue
            files += 1
            previous = ""
            for index, chunk in enumerate(self._chunks(text)):
                node_id = (
                    f"doc:{_hash(str(path))}:{index}:"
                    f"{_hash(chunk)}"
                )
                props: Dict[str, Any] = {
                    "path": str(path),
                    "title": path.name,
                    "content": chunk,
                    "chunk_index": index,
                    "created_at": _utc_now(),
                }
                relationships: List[
                    Dict[str, str]
                ] = []
                if previous:
                    relationships.append(
                        {
                            "from": previous,
                            "type": "FOLLOWED_BY",
                            "to": node_id,
                        }
                    )
                self._enqueue(
                    {
                        "type": "upsert",
                        "nodes": [
                            {
                                "id": node_id,
                                "kind": "document_chunk",
                                "props": props,
                            }
                        ],
                        "relationships": relationships,
                    }
                )
                previous = node_id
                chunks += 1
        return {
            "ok": True,
            "files": files,
            "chunks": chunks,
            "skipped": skipped,
        }

    def _tool_query(
        self,
        args: Dict[str, Any],
    ) -> Dict[str, Any]:
        cypher = str(args.get("cypher") or "").strip()
        if not cypher:
            raise ValueError("cypher is required")
        if _WRITE_CYPHER.search(cypher):
            raise ValueError(
                "kg_query is read-only; "
                "write clauses are not allowed"
            )
        if not re.match(
            r"^(MATCH|OPTIONAL\s+MATCH|RETURN|WITH|UNWIND)\b",
            cypher,
            re.IGNORECASE,
        ):
            raise ValueError(
                "unsupported read-only Cypher statement"
            )
        limit = max(
            1,
            min(int(args.get("limit") or 50), 200),
        )
        with self._session() as session:
            records = session.run(
                cypher,
                parameters=args.get("parameters") or {},
            )
            rows = [
                dict(record)
                for _, record in zip(range(limit), records)
            ]
        return {
            "ok": True,
            "rows": rows,
            "limit": limit,
        }

    def handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs,
    ) -> str:
        try:
            if not self._available:
                raise RuntimeError(
                    "knowledge_graph provider is not connected"
                )
            if tool_name == "kg_search":
                return _json_result(
                    {
                        "ok": True,
                        "results": self._search(
                            str(args.get("query") or ""),
                            int(args.get("top_k") or 8),
                        ),
                    }
                )
            if tool_name == "kg_remember":
                return _json_result(
                    self._tool_remember(args)
                )
            if tool_name == "kg_index_docs":
                return _json_result(
                    self._tool_index_docs(args)
                )
            if tool_name == "kg_query":
                return _json_result(
                    self._tool_query(args)
                )
            if tool_name == "kg_forget":
                node_id = str(args.get("node_id") or "")
                if not node_id:
                    raise ValueError("node_id is required")
                with self._session() as session:
                    summary = session.run(
                        """
                        MATCH (n:KgNode {id: $id})
                        WITH collect(n) AS nodes,
                             count(n) AS deleted
                        FOREACH (
                            node IN nodes |
                            DETACH DELETE node
                        )
                        RETURN deleted
                        """,
                        id=node_id,
                    ).single()
                return _json_result(
                    {
                        "ok": True,
                        "deleted": int(
                            summary["deleted"]
                            if summary
                            else 0
                        ),
                    }
                )
            if tool_name == "kg_status":
                return _json_result(
                    {
                        "ok": True,
                        "connected": self._available,
                        "session_id": self._session_id,
                        "queue_depth": (
                            self._queue.pending_count()
                            if self._queue
                            else 0
                        ),
                        "reasoning_capture": self._as_bool(
                            self._cfg.get(
                                "capture_reasoning"
                            )
                        ),
                        "tool_argument_capture": self._as_bool(
                            self._cfg.get(
                                "capture_tool_arguments"
                            )
                        ),
                        "tool_result_capture": self._as_bool(
                            self._cfg.get(
                                "capture_tool_results"
                            )
                        ),
                    }
                )
            raise ValueError(
                "unknown knowledge graph tool: "
                f"{tool_name}"
            )
        except Exception as exc:
            return _json_result(
                {
                    "ok": False,
                    "error": str(exc),
                }
            )

    def backup_paths(self) -> List[str]:
        return []

    def shutdown(self) -> None:
        if self._queue:
            self._queue.close(timeout=10.0)
            self._queue = None
        if self._driver:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None
        self._available = False


def register(ctx) -> None:
    ctx.register_memory_provider(
        KnowledgeGraphMemoryProvider()
    )
