"""Neo4j-backed graph store for the knowledge-graph memory provider.

Stores every captured artefact (session, user/assistant message, chain-of-thought
reasoning, tool call, idea, external entity) as a node, linked by typed
relationships. Searchable nodes carry an ``embedding`` (list of floats) and the
shared ``KgNode`` label; a native Neo4j vector index serves semantic search,
while Cypher relationship traversal serves graph ("what is this idea connected to")
queries. The two compose: start from a vector hit, then walk the graph.

Connection params come from config.yaml ``knowledge_graph`` or the
``NEO4J_*`` env vars. The store is crash-safe by construction — providers
write through a durable queue, so a lost connection only delays ingestion.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_VECTOR_INDEX = "kg_vec"
_META_NODE = "KgMeta"


class Neo4jGraphStore:
    """Thin wrapper over the official Neo4j driver."""

    def __init__(
        self,
        uri: str = "",
        user: str = "",
        password: str = "",
        database: str = "neo4j",
        max_connection_lifetime: int = 3600,
    ) -> None:
        self.uri = uri or "bolt://localhost:7687"
        self.user = user or "neo4j"
        self.password = password or ""
        self.database = database or "neo4j"
        self._max_lifetime = max_connection_lifetime
        self._driver = None
        self._lock = threading.Lock()
        self._index_dimension: Optional[int] = None

    # -- connection -----------------------------------------------------------

    def connect(self) -> bool:
        try:
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self.uri,
                auth=(self.user, self.password) if self.password else None,
                max_connection_lifetime=self._max_lifetime,
            )
            # Validate with a tiny read.
            with self._driver.session(database=self.database) as s:
                s.run("RETURN 1 AS x").consume()
            logger.info("Knowledge-graph connected to Neo4j at %s", self.uri)
            return True
        except Exception as exc:
            logger.warning("Knowledge-graph Neo4j connect failed: %s", exc)
            self._driver = None
            return False

    @property
    def connected(self) -> bool:
        if self._driver is None:
            return False
        try:
            with self._driver.session(database=self.database) as s:
                s.run("RETURN 1 AS x").consume()
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None

    def _session(self):
        return self._driver.session(database=self.database)

    # -- schema ---------------------------------------------------------------

    def ensure_schema(self, dimension: Optional[int] = None) -> None:
        if not self.connected:
            return
        try:
            with self._session() as s:
                # Unique id constraint (idempotent).
                s.run(
                    "CREATE CONSTRAINT kg_node_id IF NOT EXISTS "
                    "FOR (n:KgNode) REQUIRE n.id IS UNIQUE"
                ).consume()
                s.run(
                    "CREATE CONSTRAINT kg_meta_key IF NOT EXISTS "
                    "FOR (m:KgMeta) REQUIRE m.key IS UNIQUE"
                ).consume()
            # Vector index — needs a known dimension.
            if dimension and dimension > 0:
                self._create_vector_index(dimension)
        except Exception as exc:
            logger.warning("Knowledge-graph schema setup issue: %s", exc)

    def _create_vector_index(self, dimension: int) -> None:
        if self._index_dimension == dimension and self._vector_index_exists():
            return
        try:
            with self._session() as s:
                # Drop any prior index so a dimension change applies.
                try:
                    s.run(f"DROP INDEX {_VECTOR_INDEX} IF EXISTS").consume()
                except Exception:
                    pass
                s.run(
                    f"CREATE VECTOR INDEX {_VECTOR_INDEX} IF NOT EXISTS "
                    f"FOR (n:KgNode) ON (n.embedding) "
                    f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dimension}, "
                    f"`vector.similarity_function`: 'cosine'}}}}"
                ).consume()
            self._index_dimension = dimension
            self._set_meta("embedding_dim", dimension)
            logger.info("Knowledge-graph vector index ready (dim=%d)", dimension)
        except Exception as exc:
            logger.warning("Knowledge-graph vector index creation failed: %s", exc)

    def _vector_index_exists(self) -> bool:
        try:
            with self._session() as s:
                rec = s.run(
                    "SHOW INDEXES YIELD name WHERE name = $name "
                    "RETURN count(*) AS c",
                    name=_VECTOR_INDEX,
                ).single()
                return bool(rec and rec["c"])
        except Exception:
            return False

    def _set_meta(self, key: str, value: Any) -> None:
        try:
            with self._session() as s:
                s.run(
                    f"MERGE (m:{_META_NODE} {{key: $key}}) SET m.value = $value",
                    key=key, value=value,
                ).consume()
        except Exception:
            pass

    def get_meta(self, key: str) -> Any:
        try:
            with self._session() as s:
                rec = s.run(
                    f"MATCH (m:{_META_NODE} {{key: $key}}) RETURN m.value AS v",
                    key=key,
                ).single()
                return rec["v"] if rec else None
        except Exception:
            return None

    # -- writes ---------------------------------------------------------------

    def merge_node(
        self,
        node_id: str,
        kind: str,
        labels: List[str],
        props: Dict[str, Any],
        embedding: Optional[List[float]] = None,
    ) -> None:
        if not self.connected:
            return
        # Build label string. Every knowledge-graph node carries the KgNode
        # label so the unique-id constraint index is used for MERGE/MATCH —
        # critical when sharing a Neo4j database with unrelated data.
        all_labels = list(dict.fromkeys([kind] + labels + ["KgNode"]))
        label_str = ":".join(all_labels)
        params: Dict[str, Any] = {"id": node_id}
        set_clauses = []
        for k, v in (props or {}).items():
            # Neo4j's driver cannot bind dict/list property values directly;
            # serialise them to JSON so nested structures are stored losslessly
            # as strings rather than raising "type 'dict' is not supported".
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            params[f"p_{k}"] = v
            set_clauses.append(f"n.{k} = $p_{k}")
        if embedding:
            params["embedding"] = list(embedding)
            set_clauses.append("n.embedding = $embedding")
        set_clauses.append("n.kind = $kind_const")
        params["kind_const"] = kind
        query = (
            f"MERGE (n:{label_str} {{id: $id}}) "
            f"SET {', '.join(set_clauses)}"
        )
        try:
            with self._session() as s:
                s.run(query, **params).consume()
        except Exception as exc:
            logger.warning("kg merge_node(%s) failed: %s", node_id, exc)

    def bulk_merge_nodes(
        self,
        rows: List[Dict[str, Any]],
        batch_size: int = 500,
    ) -> int:
        """Merge many nodes in batched ``UNWIND`` statements (fast path for imports).

        Each row: ``{id, kind, labels (list[str]), props (dict), embedding (list|None)}``.
        Labels (e.g. ``KgSession``, ``doc``) are stamped via a ``SET n:L1:L2``
        clause. The embedding is applied only when present. No APOC required.
        Returns the number of rows written.

        Note: a node can only carry ONE label stamp per statement, so rows are
        grouped by their (safe, code-generated) label string and one UNWIND is
        emitted per distinct label-set. Label names are produced by our own code
        (never user input), so interpolating them into the SET clause is safe.
        """
        if not self.connected or not rows:
            return 0
        # Group rows by their label string so each UNWIND carries a fixed SET.
        by_labels: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            labels = list(r.get("labels") or [])
            all_labels = list(dict.fromkeys([r["kind"]] + labels + ["KgNode"]))
            key = ":".join(all_labels)
            by_labels.setdefault(key, []).append(r)

        written = 0
        for label_str, group in by_labels.items():
            for start in range(0, len(group), batch_size):
                chunk = group[start:start + batch_size]
                params = [{
                    "id": r["id"],
                    "kind": r["kind"],
                    "props": dict(r.get("props") or {}),
                    "embedding": list(r["embedding"]) if r.get("embedding") else None,
                } for r in chunk]
                query = (
                    "UNWIND $rows AS row "
                    "MERGE (n:KgNode {id: row.id}) "
                    f"SET n:{label_str} "
                    "SET n += row.props, n.kind = row.kind "
                    "WITH n, row "
                    "WHERE row.embedding IS NOT NULL "
                    "SET n.embedding = row.embedding"
                )
                try:
                    with self._session() as s:
                        s.run(query, rows=params).consume()
                    written += len(chunk)
                except Exception as exc:
                    logger.warning("kg bulk_merge_nodes failed: %s", exc)
        return written

    def bulk_merge_relationships(
        self,
        rows: List[Dict[str, Any]],
        batch_size: int = 2000,
    ) -> int:
        """Merge many relationships in batched ``UNWIND`` statements.

        Relationship types are literal tokens (not parameterizable), so rows are
        grouped by their type and one UNWIND is emitted per distinct type. Type
        names come from our own code, so interpolating them is safe.
        """
        if not self.connected or not rows:
            return 0
        by_rel: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            by_rel.setdefault(r["rel"], []).append(r)

        written = 0
        for rel_type, group in by_rel.items():
            for start in range(0, len(group), batch_size):
                chunk = group[start:start + batch_size]
                params = [{"src": r["src"], "dst": r["dst"]} for r in chunk]
                query = (
                    "UNWIND $rows AS row "
                    "MATCH (a:KgNode {id: row.src}) "
                    "MATCH (b:KgNode {id: row.dst}) "
                    f"MERGE (a)-[r:{rel_type}]->(b)"
                )
                try:
                    with self._session() as s:
                        s.run(query, rows=params).consume()
                    written += len(chunk)
                except Exception as exc:
                    logger.warning("kg bulk_merge_relationships failed: %s", exc)
        return written

    def merge_relationship(
        self,
        src_id: str,
        rel_type: str,
        dst_id: str,
        props: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.connected:
            return
        params: Dict[str, Any] = {"src": src_id, "dst": dst_id}
        set_clause = ""
        if props:
            pc = []
            for k, v in props.items():
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)
                params[f"r_{k}"] = v
                pc.append(f"r.{k} = $r_{k}")
            set_clause = "SET " + ", ".join(pc)
        query = (
            "MATCH (a:KgNode {id: $src}) "
            "MATCH (b:KgNode {id: $dst}) "
            f"MERGE (a)-[r:{rel_type}]->(b) {set_clause}"
        )
        try:
            with self._session() as s:
                s.run(query, **params).consume()
        except Exception as exc:
            logger.debug("kg rel (%s)->(%s) failed: %s", src_id, dst_id, exc)

    # -- reads ----------------------------------------------------------------

    def vector_search(
        self,
        embedding: List[float],
        top_k: int = 8,
        kinds: Optional[List[str]] = None,
        cutoff: float = 0.0,
    ) -> List[Dict[str, Any]]:
        if not self.connected or not embedding:
            return []
        if not self._vector_index_exists():
            return []
        kind_filter = ""
        params: Dict[str, Any] = {"vec": list(embedding), "k": int(top_k)}
        if kinds:
            kind_filter = "WHERE n.kind IN $kinds"
            params["kinds"] = list(kinds)
        query = (
            f"CALL db.index.vector.queryNodes('{_VECTOR_INDEX}', $k, $vec) "
            f"YIELD node AS n, score AS score "
            f"{kind_filter} "
            f"RETURN n.id AS id, n.kind AS kind, n.content AS content, "
            f"n.path AS path, n.heading AS heading, n.value AS value, "
            f"n.session_id AS session_id, n.created_at AS created_at, score "
            f"ORDER BY score DESC"
        )
        out: List[Dict[str, Any]] = []
        try:
            with self._session() as s:
                for rec in s.run(query, **params):
                    score = float(rec["score"])
                    if cutoff and score < cutoff:
                        continue
                    out.append({
                        "id": rec["id"],
                        "kind": rec["kind"],
                        "content": rec["content"],
                        "path": rec["path"],
                        "heading": rec["heading"],
                        "value": rec["value"],
                        "session_id": rec["session_id"],
                        "created_at": rec["created_at"],
                        "score": score,
                    })
        except Exception as exc:
            logger.warning("kg vector_search failed: %s", exc)
        return out

    def neighbors(
        self,
        node_id: str,
        rel_types: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        if not self.connected:
            return []
        rel_clause = ""
        if rel_types:
            rel_clause = "|".join(f":{r}" for r in rel_types)
        query = (
            f"MATCH (n:KgNode {{id: $id}})-[{rel_clause}]-(m) "
            f"RETURN DISTINCT m.id AS id, m.kind AS kind, m.content AS content, "
            f"labels(m) AS labels LIMIT $limit"
        )
        out: List[Dict[str, Any]] = []
        try:
            with self._session() as s:
                for rec in s.run(query, id=node_id, limit=int(limit)):
                    out.append({
                        "id": rec["id"],
                        "kind": rec["kind"],
                        "content": rec["content"],
                        "labels": rec["labels"],
                    })
        except Exception as exc:
            logger.warning("kg neighbors(%s) failed: %s", node_id, exc)
        return out

    def run_cypher(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if not self.connected:
            return []
        out: List[Dict[str, Any]] = []
        try:
            with self._session() as s:
                for rec in s.run(query, **(params or {})):
                    out.append(dict(rec))
        except Exception as exc:
            logger.warning("kg cypher failed: %s", exc)
            raise
        return out

    def stats(self) -> Dict[str, Any]:
        if not self.connected:
            return {"connected": False}
        try:
            with self._session() as s:
                rec = s.run(
                    "MATCH (n:KgNode) RETURN count(n) AS nodes"
                ).single()
                nodes = int(rec["nodes"]) if rec else 0
                rel = s.run(
                    "MATCH (:KgNode)-[r]->(:KgNode) RETURN count(r) AS c"
                ).single()
                rels = int(rel["c"]) if rel else 0
                by_kind = s.run(
                    "MATCH (n:KgNode) RETURN n.kind AS kind, count(*) AS c"
                ).data()
            return {
                "connected": True,
                "nodes": nodes,
                "relationships": rels,
                "by_kind": {r["kind"]: r["c"] for r in by_kind},
                "embedding_dim": self.get_meta("embedding_dim"),
            }
        except Exception as exc:
            logger.warning("kg stats failed: %s", exc)
            return {"connected": True, "error": str(exc)}
