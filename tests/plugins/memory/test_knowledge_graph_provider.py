from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from plugins.memory.knowledge_graph import (
    KnowledgeGraphMemoryProvider,
    _DurableQueue,
)


class _CaptureQueue:
    def __init__(self):
        self.items = []

    def enqueue(self, payload):
        self.items.append(payload)
        return len(self.items)

    def pending_count(self):
        return len(self.items)


def _provider(**config):
    provider = KnowledgeGraphMemoryProvider()
    provider._available = True
    provider._session_id = "session-1"
    provider._cfg = {
        "capture_reasoning": False,
        "capture_tool_arguments": False,
        **config,
    }
    provider._queue = _CaptureQueue()
    return provider


def test_sync_turn_consumes_public_messages_contract_without_embedding_calls(monkeypatch):
    provider = _provider()

    def fail_if_called(_text):
        raise AssertionError("sync_turn must not perform embedding network calls")

    monkeypatch.setattr(provider, "_embed", fail_if_called)
    provider.sync_turn(
        "hello",
        "done",
        session_id="session-1",
        messages=[
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "done",
                "reasoning": "private reasoning",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command":"cat ~/.env"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "blocked",
            },
        ],
    )

    assert len(provider._queue.items) == 2
    turn = provider._queue.items[1]
    assert turn["type"] == "upsert"
    assert not any(node["kind"] == "reasoning" for node in turn["nodes"])

    tool_node = next(node for node in turn["nodes"] if node["kind"] == "tool_call")
    assert tool_node["props"]["name"] == "terminal"
    assert tool_node["props"]["arguments"].startswith("[redacted")
    assert any(rel["type"] == "PRODUCED" for rel in turn["relationships"])


def test_reasoning_and_tool_arguments_require_explicit_opt_in():
    provider = _provider(capture_reasoning=True, capture_tool_arguments=True)
    nodes, relationships = provider._nodes_from_messages(
        [
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "reasoning text",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"notes.md"}',
                        },
                    }
                ],
            },
        ],
        "session-1",
    )

    reasoning = next(node for node in nodes if node["kind"] == "reasoning")
    assert reasoning["props"]["content"] == "reasoning text"
    tool_node = next(node for node in nodes if node["kind"] == "tool_call")
    assert "notes.md" in tool_node["props"]["arguments"]
    assert any(rel["type"] == "REASONED" for rel in relationships)


def test_read_only_cypher_rejects_mutation_before_driver_access():
    provider = _provider()
    result = json.loads(
        provider.handle_tool_call(
            "kg_query",
            {"cypher": "MATCH (n) DETACH DELETE n RETURN count(n)"},
        )
    )

    assert result["ok"] is False
    assert "read-only" in result["error"]


def test_save_config_is_profile_scoped(tmp_path):
    provider = KnowledgeGraphMemoryProvider()
    provider.save_config(
        {
            "enabled": True,
            "uri": "bolt://neo4j.internal:7687",
            "embeddings_base_urls": "http://xwing:1234/v1,http://tie:1234/v1",
        },
        str(tmp_path),
    )

    config_path = tmp_path / "knowledge_graph.json"
    assert config_path.exists()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["enabled"] is True
    assert saved["embeddings_base_urls"] == [
        "http://xwing:1234/v1",
        "http://tie:1234/v1",
    ]


def test_is_available_is_configuration_only_and_makes_no_network_call(monkeypatch):
    provider = KnowledgeGraphMemoryProvider()
    monkeypatch.setattr(
        provider,
        "_load_config",
        lambda: {"enabled": True, "uri": "bolt://localhost:7687"},
    )
    monkeypatch.setattr(
        "plugins.memory.knowledge_graph.importlib.util.find_spec",
        lambda name: object() if name == "neo4j" else None,
    )

    assert provider.is_available() is True


def test_durable_queue_deletes_rows_only_after_success(tmp_path):
    observed = []
    queue = _DurableQueue(tmp_path / "queue.db", observed.append)
    queue.enqueue({"type": "upsert", "nodes": [], "relationships": []})

    deadline = time.time() + 3
    while queue.pending_count() and time.time() < deadline:
        time.sleep(0.02)

    assert observed == [{"type": "upsert", "nodes": [], "relationships": []}]
    assert queue.pending_count() == 0
    queue.close()


def test_document_chunks_overlap_without_infinite_loop():
    provider = KnowledgeGraphMemoryProvider()
    text = " ".join(f"token-{index}" for index in range(1000))
    chunks = list(provider._chunks(text, size=300, overlap=50))

    assert len(chunks) > 1
    assert all(chunks)
    assert sum(len(chunk) for chunk in chunks) > len(text)
