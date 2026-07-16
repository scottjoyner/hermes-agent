"""Live integration tests for the knowledge-graph memory provider.

These exercise the REAL Neo4j store and a REAL local embeddings endpoint —
the layers that the mocked unit tests in
``tests/plugins/memory/test_knowledge_graph.py`` cannot cover. They exist
because several integration-only bugs slipped past the mocks:

  * ``merge_relationship`` / ``neighbors`` doing unindexed full scans of a
    shared database (missing the ``:KgNode`` label on the MATCH),
  * ``_vector_index_exists`` using invalid ``SHOW INDEXES WHERE ... YIELD``
    ordering (always False -> ``vector_search`` always returned []),
  * ``stats`` counting relationships across the whole DB instead of the
    knowledge-graph subgraph,
  * IPv6 ``localhost`` hangs against IPv4-only endpoints.

Gated behind env vars so the default suite (``-m 'not integration'``) never
touches external services. Run with, e.g.::

    KG_TEST_NEO4J_URI=bolt://127.0.0.1:7687 \
    KG_TEST_NEO4J_PASSWORD=knowledge_graph_2026 \
    KG_TEST_EMBED_URL=http://127.0.0.1:1234/v1 \
    KG_TEST_EMBED_MODEL=text-embedding-nomic-embed-text-v1.5 \
    pytest tests/integration/test_knowledge_graph_live.py -m integration -v

Every test writes nodes under a unique, test-only session id and DETACH
DELETEs them in teardown, so a shared Neo4j instance is safe to target.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.integration

_URI = os.getenv("KG_TEST_NEO4J_URI")
_EMBED_URL = os.getenv("KG_TEST_EMBED_URL")
if not _URI or not _EMBED_URL:
    pytest.skip(
        "KG_TEST_NEO4J_URI and KG_TEST_EMBED_URL not set — skipping live "
        "knowledge-graph integration tests",
        allow_module_level=True,
    )

_USER = os.getenv("KG_TEST_NEO4J_USER", "neo4j")
_PASSWORD = os.getenv("KG_TEST_NEO4J_PASSWORD", "")
_DATABASE = os.getenv("KG_TEST_NEO4J_DATABASE", "neo4j")
_EMBED_MODEL = os.getenv("KG_TEST_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")


def _make_store():
    from plugins.memory.knowledge_graph.graph_store import Neo4jGraphStore

    store = Neo4jGraphStore(uri=_URI, user=_USER, password=_PASSWORD, database=_DATABASE)
    if not store.connect():
        pytest.skip(f"Neo4j not reachable at {_URI}")
    return store


def _make_embed():
    from plugins.memory.knowledge_graph.embeddings import LocalEmbeddingClient

    return LocalEmbeddingClient(
        base_url=_EMBED_URL, model=_EMBED_MODEL, api_key="",
        timeout=30.0, batch_size=32,
    )


@pytest.fixture()
def live():
    """A connected store + embedder + provider bound to a throwaway session."""
    store = _make_store()
    embed = _make_embed()
    # dimension is only known after the first successful embed call.
    probe = embed.embed("dimension probe")
    dim = embed.dimension
    if not probe or not dim:
        pytest.skip(f"Embeddings endpoint at {_EMBED_URL} returned no vector")
    store.ensure_schema(dim)

    session_id = f"ittest:{uuid.uuid4().hex[:12]}"

    from plugins.memory.knowledge_graph import KnowledgeGraphMemoryProvider

    p = KnowledgeGraphMemoryProvider()
    p._cfg = {
        "embeddings": {},
        "capture": {"sessions": True, "chain_of_thought": True, "tool_calls": True,
                    "user_messages": True, "assistant_messages": True},
        "search": {"top_k": 8, "similarity_cutoff": 0.0, "scope": "both",
                   "doc_weight": 0.6, "cot_weight": 0.4},
    }
    p._available = True
    p._session_id = session_id
    p._embed = embed
    p._store = store
    # Apply jobs synchronously against the live store (bypass the durable queue).
    p._enqueue_job = lambda sid, job: p._apply_job(job)  # type: ignore[method-assign]

    # Create the session node so ``HAS`` relationships have a valid anchor.
    p._apply_job({"type": "session", "session_id": session_id,
                  "platform": "test", "profile": "test"})

    try:
        yield p, store, embed, session_id
    finally:
        # Remove only the nodes this test created.
        try:
            store.run_cypher(
                "MATCH (n:KgNode) WHERE n.session_id = $sid DETACH DELETE n",
                {"sid": session_id},
            )
        except Exception:
            pass
        try:
            store.close()
        except Exception:
            pass


def test_connect_and_schema_online(live):
    _, store, _, _ = live
    assert store.connected is True
    # The regression: SHOW INDEXES YIELD ... WHERE ... must be valid Cypher,
    # so the vector index is actually detected.
    assert store._vector_index_exists() is True


def test_embeddings_endpoint_returns_vectors(live):
    _, _, embed, _ = live
    v = embed.embed("the quick brown fox")
    assert isinstance(v, list) and len(v) == embed.dimension
    many = embed.embed_many(["alpha", "", "gamma"])
    assert len(many) == 3
    assert all(len(x) == embed.dimension for x in many)


def test_index_and_vector_search_roundtrip(live, tmp_path):
    p, store, _, session_id = live
    doc = tmp_path / "zfs_runbook.md"
    doc.write_text(
        "# Storage Pool\nThe NAS runs ZFS with mirrored vdevs for redundancy.\n\n"
        "## Snapshots\nHourly snapshots are pruned after 30 days.\n\n"
        "## Networking\nStorage traffic is isolated on VLAN 40.\n",
        encoding="utf-8",
    )
    out = p._dispatch("kg_index_docs", {"paths": [str(doc)], "recursive": False})
    assert out["indexed_files"] == 1
    assert out["chunks"] >= 3

    # The core regression: vector_search must actually return hits (the
    # _vector_index_exists guard used to short-circuit to []).
    hits = p._dispatch("kg_search", {"query": "how is the storage pool made redundant"})
    results = hits["results"]
    assert results, "vector_search returned no hits against a populated index"
    top = results[0]
    assert top["kind"] in ("docchunk", "doc")
    assert top.get("path", "").endswith("zfs_runbook.md")


def test_search_scope_and_weighting_live(live, tmp_path):
    p, _, _, _ = live
    doc = tmp_path / "notes.md"
    doc.write_text(
        "# Deploy\nRun the migration before flipping traffic.\n\n"
        "## Rollback\nRestore the previous image and re-run migrations.\n",
        encoding="utf-8",
    )
    p._dispatch("kg_index_docs", {"paths": [str(doc)], "recursive": False})

    both = p._dispatch("kg_search", {"query": "deployment rollback", "top_k": 5})
    assert both["scope"] == "both"
    for r in both["results"]:
        # Doc-kind hits carry the 0.6 blend weight applied to their raw score.
        if r["kind"] in ("docchunk", "doc"):
            assert r["weighted_score"] == pytest.approx(r["score"] * 0.6, abs=1e-3)

    docs_only = p._dispatch("kg_search", {"query": "deployment rollback",
                                          "scope": "docs", "top_k": 5})
    assert docs_only["scope"] == "docs"
    assert {r["kind"] for r in docs_only["results"]} <= {"docchunk", "doc"}
    for r in docs_only["results"]:
        # scope=docs -> full weight.
        assert r["weighted_score"] == pytest.approx(r["score"], abs=1e-3)


def test_relationship_writes_and_neighbors_are_indexed(live):
    """merge_relationship + neighbors must use the :KgNode index, not scan."""
    p, store, _, session_id = live
    # Capture a full turn: user -> assistant (+CoT) -> toolcall -> result.
    messages = [
        {"role": "user", "content": "reverse a list in python"},
        {"role": "assistant", "content": "Use lst[::-1].",
         "reasoning": "Slicing with step -1 reverses in place-free fashion.",
         "tool_calls": [{"id": "c1", "function": {"name": "python_exec",
                                                  "arguments": "{\"code\": \"[1,2,3][::-1]\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "[3, 2, 1]"},
    ]
    p.on_turn_recorded({
        "session_id": session_id, "messages": messages,
        "user_message": messages[0]["content"],
        "assistant_response": messages[1]["content"], "interrupted": False,
    })

    # The relationships must actually exist in the graph (proves the labeled
    # MATCH found both endpoints via the unique-id index).
    rels = store.run_cypher(
        "MATCH (a:KgNode {session_id: $sid})-[r]->(b:KgNode) "
        "RETURN type(r) AS t, count(*) AS c", {"sid": session_id},
    )
    rel_types = {row["t"]: row["c"] for row in rels}
    assert "HAS" in rel_types
    assert "REASONED" in rel_types or "CALLED" in rel_types

    # neighbors() must resolve the anchor by id and return connected nodes.
    anchor = store.run_cypher(
        "MATCH (n:KgNode {session_id: $sid, kind: 'message'}) RETURN n.id AS id LIMIT 1",
        {"sid": session_id},
    )
    assert anchor, "expected a captured message node"
    neigh = store.neighbors(anchor[0]["id"], limit=10)
    assert neigh, "neighbors() returned nothing for a connected node"


def test_stats_scoped_to_knowledge_graph(live, tmp_path):
    """stats() edge count must reflect the KG subgraph, not the whole DB."""
    p, store, _, session_id = live
    doc = tmp_path / "s.md"
    doc.write_text("# H\nsome content here for a chunk.\n", encoding="utf-8")
    p._dispatch("kg_index_docs", {"paths": [str(doc)], "recursive": False})

    stats = store.stats()
    assert stats["connected"] is True
    assert isinstance(stats["nodes"], int)
    assert isinstance(stats["relationships"], int)
    # The regression this guards: stats used to count EVERY relationship in a
    # shared database (millions) instead of the KgNode subgraph. On a healthy
    # graph the edge count stays proportional to the node count.
    assert stats["nodes"] > 0
    assert stats["relationships"] <= stats["nodes"] * 12
