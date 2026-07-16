"""Unit tests for the knowledge-graph memory provider.

These exercise the capture/query LOGIC without touching Neo4j or a local
embeddings endpoint — the store and embedder are mocked, and the durable
queue is bypassed so we can assert on the job payloads directly.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from plugins.memory.knowledge_graph import (
    KnowledgeGraphMemoryProvider,
    _node,
    _rel,
)


def _make_provider() -> KnowledgeGraphMemoryProvider:
    p = KnowledgeGraphMemoryProvider()
    p._cfg = {
        "enabled": True,
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
        "embeddings": {"backend": "local", "base_url": "", "model": "",
                      "api_key": "", "dimensions": 0, "batch_size": 32, "timeout": 30.0},
    }
    p._available = True
    p._session_id = "sess-1"
    p._embed = mock.MagicMock()
    p._embed.dimension = 384
    p._store = mock.MagicMock()
    p._store.vector_search.return_value = [
        {"id": "msg:sess-1:1:aaaaaaaa", "kind": "reasoning",
         "content": "think hard", "session_id": "sess-1", "score": 0.91},
    ]
    p._store.neighbors.return_value = []
    # Bypass the durable queue; capture the job instead.
    p._enqueue_job = mock.MagicMock()  # type: ignore[method-assign]
    return p


def _last_job(p: KnowledgeGraphMemoryProvider) -> dict:
    return p._enqueue_job.call_args[0][1]


def test_turn_capture_builds_graph_edges():
    p = _make_provider()
    messages = [
        {"role": "user", "content": "How do I reverse a list in Python?"},
        {"role": "assistant", "content": "Use slicing: lst[::-1].",
         "reasoning": "The user wants reversal. Slicing with step -1 is idiomatic.",
         "tool_calls": [{"id": "call_1", "function": {"name": "python_exec",
                                                            "arguments": "{\"code\": \"[1,2,3][::-1]\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": "Result: [3, 2, 1]"},
    ]
    p.on_turn_recorded({
        "session_id": "sess-1", "messages": messages,
        "user_message": messages[0]["content"],
        "assistant_response": messages[1]["content"], "interrupted": False,
    })

    job = _last_job(p)
    kinds = {n["kind"] for n in job["nodes"]}
    assert "message" in kinds
    assert "reasoning" in kinds
    assert "toolcall" in kinds

    rels = {(r["src"].split(":")[0], r["rel"], r["dst"].split(":")[0]) for r in job["rels"]}
    # session -> message/ reasoning/ toolcall
    assert ("sess", "HAS", "msg") in rels
    assert ("sess", "HAS", "reasoning") in rels
    assert ("sess", "HAS", "toolcall") in rels
    # user -> assistant
    assert ("msg", "FOLLOWED_BY", "msg") in rels
    # assistant -> reasoning (CoT)
    assert ("msg", "REASONED", "reasoning") in rels
    # assistant -> toolcall, toolcall -> result
    assert ("msg", "CALLED", "toolcall") in rels
    assert ("toolcall", "PRODUCED", "toolresult") in rels


def test_cot_capture_toggle_off():
    p = _make_provider()
    p._cfg["capture"]["chain_of_thought"] = False
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "reasoning": "secret thought"},
    ]
    p.on_turn_recorded({"session_id": "sess-1", "messages": messages,
                        "user_message": "hi", "assistant_response": "hello",
                        "interrupted": False})
    kinds = {n["kind"] for n in _last_job(p)["nodes"]}
    assert "reasoning" not in kinds


def test_remember_idea_links_entities():
    p = _make_provider()
    p._session_id = "sess-1"
    out = p._dispatch("kg_remember_idea", {
        "content": "We should index the NAS docs in the graph.",
        "tags": ["architecture"],
        "links": [
            {"kind": "repo", "value": "github.com/scott/hermes-agent"},
            {"kind": "filepath", "value": "/mnt/nas/docs/architecture.md"},
        ],
    })
    assert out["idea_id"].startswith("idea:")
    assert out["linked"] == 2
    job = _last_job(p)
    idea_nodes = [n for n in job["nodes"] if n["kind"] == "idea"]
    entity_nodes = [n for n in job["nodes"] if n["kind"] == "entity"]
    assert len(idea_nodes) == 1
    assert len(entity_nodes) == 2
    rels = {r["rel"] for r in job["rels"]}
    assert "LINKS_TO" in rels


def test_prefetch_formats_recall():
    p = _make_provider()
    p._embed.embed.return_value = [0.1] * 384
    p.queue_prefetch("reversal")
    block = p.prefetch("reversal")
    assert "Knowledge Graph recall" in block
    assert "think hard" in block
    # consumed once
    assert p.prefetch("reversal") == ""


def test_search_requires_embeddings():
    p = _make_provider()
    p._embed = None
    out = p._dispatch("kg_search", {"query": "anything"})
    assert "error" in out


def test_index_docs_creates_doc_and_chunks(tmp_path):
    p = _make_provider()
    doc = tmp_path / "architecture.md"
    doc.write_text(
        "# Overview\nThe NAS runs ZFS.\n\n## Storage\nPools are mirrored.\n\n"
        "## Networking\nVLAN 40 for storage.\n",
        encoding="utf-8",
    )
    out = p._dispatch("kg_index_docs", {"paths": [str(doc)]})
    assert out["indexed_files"] == 1
    assert out["chunks"] >= 3
    job = _last_job(p)
    kinds = {n["kind"] for n in job["nodes"]}
    assert "doc" in kinds
    assert "docchunk" in kinds
    rel_types = {r["rel"] for r in job["rels"]}
    assert "HAS_CHUNK" in rel_types


def test_idea_links_resolve_to_indexed_doc(tmp_path):
    # Index a doc, then link an idea to the same path -> same node id.
    p = _make_provider()
    doc = tmp_path / "arch.md"
    doc.write_text("# Arch\nDetails.\n", encoding="utf-8")
    p._dispatch("kg_index_docs", {"paths": [str(doc)]})
    doc_job = _last_job(p)
    doc_id = [n["id"] for n in doc_job["nodes"] if n["kind"] == "doc"][0]

    p._dispatch("kg_remember_idea", {
        "content": "Mirror the pool config.",
        "links": [{"kind": "doc", "value": str(doc)}],
    })
    idea_job = _last_job(p)
    entity_ids = [n["id"] for n in idea_job["nodes"] if n["kind"] == "entity"]
    # The idea's doc-link entity MUST be the same node as the indexed Doc.
    assert doc_id in entity_ids


def test_harness_weak_injects_full_text_and_nudge(tmp_path):
    p = _make_provider()
    p._model = "anthropic/claude-haiku-4-5"  # weak tier
    assert p._tier() == "weak"
    p._embed.embed.return_value = [0.1] * 384
    p._store.vector_search.return_value = [{
        "id": "chunk:abc:0", "kind": "docchunk", "content": "ZFS mirror pool details",
        "path": "/mnt/nas/docs/arch.md", "heading": "Storage",
        "value": None, "session_id": "sess-1", "score": 0.93,
    }]
    p.queue_prefetch("how is the pool mirrored")
    block = p.prefetch("how is the pool mirrored")
    assert "READ THESE SOURCE FILES" in block
    assert "/mnt/nas/docs/arch.md" in block
    # Full text is injected for weak models, not just a pointer.
    assert "ZFS mirror pool details" in block


def test_harness_strong_uses_pointer_only(tmp_path):
    p = _make_provider()
    p._model = "gpt-5"  # strong tier -> terse pointer
    assert p._tier() == "strong"
    p._embed.embed.return_value = [0.1] * 384
    p._store.vector_search.return_value = [{
        "id": "chunk:abc:0", "kind": "docchunk", "content": "ZFS mirror pool details",
        "path": "/mnt/nas/docs/arch.md", "heading": "Storage",
        "value": None, "session_id": "sess-1", "score": 0.93,
    }]
    p.queue_prefetch("how is the pool mirrored")
    block = p.prefetch("how is the pool mirrored")
    # Strong model gets the path pointer, not the body text.
    assert "/mnt/nas/docs/arch.md" in block
    assert "ZFS mirror pool details" not in block


def test_auto_index_runs_at_startup(tmp_path):
    p = _make_provider()
    doc = tmp_path / "runbook.md"
    doc.write_text("# Runbook\nSteps.\n", encoding="utf-8")
    p._cfg["auto_index_docs"] = True
    p._cfg["doc_roots"] = [str(doc)]
    p._auto_index([str(doc)])
    job = _last_job(p)
    assert any(n["kind"] == "doc" for n in job["nodes"])
    assert job["type"] == "doc"


def test_forced_tier_overrides_model():
    p = _make_provider()
    # Per-profile pin: a strong model forced to the weak harness.
    p._cfg["harness"]["tier"] = "weak"
    p._model = "gpt-5"  # would auto-resolve to "strong"
    assert p._tier() == "weak"
    ts = p._tier_settings("weak")
    assert ts["read_source_nudge"] is True
    assert ts["inject_full_text"] is True


def test_chunk_markdown_groups_by_heading():
    from plugins.memory.knowledge_graph import _chunk_markdown

    md = "# A\none two\n\n## B\nthree four five\n\n## C\n" + ("x" * 5000)
    chunks = _chunk_markdown(md, max_chars=1500, overlap=200)
    heads = [h for h, _ in chunks]
    assert "A" in heads
    assert "B" in heads
    assert "C" in heads
    # The long section C must have been windowed into multiple chunks.
    assert len(chunks) > 3


# --- doc/CoT recall scope + weighting -------------------------------------

_DOC_HIT = {"id": "chunk:doc:0", "kind": "docchunk", "content": "doc body",
            "path": "/docs/a.md", "heading": "S", "session_id": "sess-1",
            "score": 0.80}
_COT_HIT = {"id": "msg:sess-1:1:bbbb", "kind": "reasoning", "content": "a thought",
            "path": "", "session_id": "sess-1", "score": 0.80}


def _kind_aware_store(p):
    """Make vector_search honor the requested kinds like Neo4j would."""
    pool = [_DOC_HIT, _COT_HIT]

    def _search(_vec, top_k=8, kinds=None, cutoff=0.0):
        ks = set(kinds) if kinds else None
        return [dict(h) for h in pool if ks is None or h["kind"] in ks]

    p._store.vector_search.side_effect = _search
    p._embed.embed.return_value = [0.1] * 384


def test_search_scope_both_blends_doc_and_cot():
    p = _make_provider()
    _kind_aware_store(p)
    out = p._dispatch("kg_search", {"query": "pool"})
    assert out["scope"] == "both"
    kinds = [r["kind"] for r in out["results"]]
    assert "docchunk" in kinds and "reasoning" in kinds
    # Equal raw scores -> doc (0.6) outranks CoT (0.4).
    assert out["results"][0]["kind"] == "docchunk"
    doc = next(r for r in out["results"] if r["kind"] == "docchunk")
    cot = next(r for r in out["results"] if r["kind"] == "reasoning")
    assert doc["weighted_score"] == pytest.approx(0.48, abs=1e-3)
    assert cot["weighted_score"] == pytest.approx(0.32, abs=1e-3)


def test_search_scope_docs_only():
    p = _make_provider()
    _kind_aware_store(p)
    out = p._dispatch("kg_search", {"query": "pool", "scope": "docs"})
    assert out["scope"] == "docs"
    kinds = {r["kind"] for r in out["results"]}
    assert kinds == {"docchunk"}


def test_search_scope_cot_only():
    p = _make_provider()
    _kind_aware_store(p)
    out = p._dispatch("kg_search", {"query": "pool", "scope": "cot"})
    assert out["scope"] == "cot"
    kinds = {r["kind"] for r in out["results"]}
    assert kinds == {"reasoning"}


def test_search_explicit_weights_override_config():
    p = _make_provider()
    _kind_aware_store(p)
    # Flip the emphasis: CoT-heavy overrides the 0.6/0.4 config default.
    out = p._dispatch("kg_search", {"query": "pool", "doc_weight": 0.2,
                                    "cot_weight": 0.8})
    assert out["results"][0]["kind"] == "reasoning"
    assert out["weights"] == {"doc": 0.2, "cot": 0.8}


def test_prefetch_uses_configured_weighting():
    p = _make_provider()
    _kind_aware_store(p)
    p.queue_prefetch("pool")
    block = p.prefetch("pool")
    assert "Knowledge Graph recall" in block
    # Both a doc pointer and the CoT snippet appear in the blended recall.
    assert "/docs/a.md" in block
    assert "a thought" in block


def test_on_delegation_captures_edges():
    p = _make_provider()
    p.on_delegation(
        task="write a parser",
        result="done",
        child_session_id="sess-child",
        goal="write a parser",
        context="use pydantic",
    )
    job = _last_job(p)
    assert job["type"] == "turn"
    kinds = {n["kind"] for n in job["nodes"]}
    assert "delegation" in kinds
    assert "session" in kinds  # child session lazily created
    rel_pairs = {(r["src"], r["rel"], r["dst"]) for r in job["rels"]}
    # Parent session -> delegation node, and delegation -> child session.
    assert any(r[1] == "DELEGATED_TO" for r in rel_pairs)
    # The result message is attached to the child session + produced by deleg.
    result_nodes = [n for n in job["nodes"] if n["kind"] == "message"]
    assert result_nodes and result_nodes[0]["props"]["content"] == "done"


def test_on_delegation_no_child_session_id_lazy_node():
    p = _make_provider()
    p.on_delegation(task="t", result="", child_session_id="", goal="t", context="")
    job = _last_job(p)
    # No result -> no message node; delegation still linked to a lazily-made node.
    assert not any(n["kind"] == "message" for n in job["nodes"])
    assert any(n["kind"] == "delegation" for n in job["nodes"])

