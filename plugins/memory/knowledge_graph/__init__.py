"""Neo4j knowledge-graph memory provider integration layer.

The implementation lives in :mod:`.provider`.  This package layer keeps the
provider aligned with the current Hermes lifecycle contract: non-primary agent
contexts can read memory but cannot mutate it, configuration booleans are
normalized safely, and transcript rewinds do not create self-lineage edges.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .provider import (
    KnowledgeGraphMemoryProvider as _BaseKnowledgeGraphMemoryProvider,
)
from .provider import _DurableQueue, logger


_READ_ONLY_CONTEXTS = {"cron", "flush", "subagent"}
_WRITE_TOOL_NAMES = {"kg_remember", "kg_index_docs", "kg_forget"}


class KnowledgeGraphMemoryProvider(_BaseKnowledgeGraphMemoryProvider):
    """Lifecycle-safe wrapper around the Neo4j provider implementation."""

    def __init__(self) -> None:
        super().__init__()
        self._write_enabled = True

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
        cfg["enabled"] = self._as_bool(cfg.get("enabled")) or self._as_bool(
            os.getenv("HERMES_KG_ENABLED", "")
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
                url.strip() for url in env_urls.split(",") if url.strip()
            ]
        elif isinstance(cfg.get("embeddings_base_urls"), str):
            cfg["embeddings_base_urls"] = [
                url.strip()
                for url in str(cfg["embeddings_base_urls"]).split(",")
                if url.strip()
            ]
        elif isinstance(cfg.get("embeddings_base_url"), str) and cfg.get(
            "embeddings_base_url"
        ):
            cfg["embeddings_base_urls"] = [str(cfg["embeddings_base_url"])]

        cfg["embeddings_model"] = os.getenv(
            "HERMES_KG_EMBEDDINGS_MODEL", ""
        ) or str(cfg.get("embeddings_model") or "")
        cfg["embeddings_api_key"] = os.getenv(
            "HERMES_KG_EMBEDDINGS_API_KEY", ""
        ) or str(cfg.get("embeddings_api_key") or "")

        for key in (
            "capture_reasoning",
            "capture_tool_arguments",
            "capture_tool_results",
        ):
            cfg[key] = self._as_bool(cfg.get(key))
        return cfg

    def initialize(self, session_id: str, **kwargs) -> None:
        self._cfg = self._load_config()
        self._session_id = session_id or ""
        self._hermes_home = Path(
            str(kwargs.get("hermes_home") or self._hermes_home)
        )
        self._platform = str(kwargs.get("platform") or "cli")
        self._model = str(kwargs.get("model") or "")
        agent_context = str(kwargs.get("agent_context") or "primary").lower()
        self._write_enabled = agent_context not in _READ_ONLY_CONTEXTS

        if not self._cfg.get("enabled"):
            return
        try:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self._cfg["uri"],
                auth=(self._cfg["user"], self._cfg["password"]),
            )
            self._driver.verify_connectivity()
            if self._write_enabled:
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
        if self._write_enabled:
            queue_path = self._hermes_home / "knowledge_graph" / "pending.db"
            self._queue = _DurableQueue(queue_path, self._apply_job)
            if self._session_id:
                self._enqueue_session(self._session_id, event="start")
        else:
            self._queue = None

    def _enqueue(self, payload: Dict[str, Any]) -> None:
        if self._write_enabled:
            super()._enqueue(payload)

    def _ensure_vector_index(self, dimensions: int) -> None:
        if self._write_enabled:
            super()._ensure_vector_index(dimensions)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas = super().get_tool_schemas()
        if self._write_enabled:
            return schemas
        return [
            schema
            for schema in schemas
            if str(schema.get("name") or "") not in _WRITE_TOOL_NAMES
        ]

    def handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs,
    ) -> str:
        if not self._write_enabled and tool_name in _WRITE_TOOL_NAMES:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        f"{tool_name} is disabled in read-only "
                        "agent contexts"
                    ),
                }
            )
        return super().handle_tool_call(tool_name, args, **kwargs)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        old_session_id = self._session_id
        self._session_id = new_session_id or self._session_id

        if rewound and self._session_id == old_session_id:
            key = self._session_id or "default"
            with self._prefetch_lock:
                self._prefetch.pop(key, None)
            return

        if not (
            self._available
            and self._write_enabled
            and new_session_id
        ):
            return

        parent = parent_session_id or (
            old_session_id if not reset else ""
        )
        if parent == new_session_id:
            parent = ""
        self._enqueue_session(
            new_session_id,
            parent_session_id=parent,
            event=str(
                kwargs.get("reason")
                or kwargs.get("event")
                or "switch"
            ),
        )


def register(ctx) -> None:
    ctx.register_memory_provider(KnowledgeGraphMemoryProvider())


__all__ = ["KnowledgeGraphMemoryProvider", "_DurableQueue", "register"]
