from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

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


class _FakeDriver:
    def __init__(self):
        self.verified = False
        self.closed = False

    def verify_connectivity(self):
        self.verified = True

    def close(self):
        self.closed = True


class _FakeGraphDatabase:
    driver_instance = _FakeDriver()

    @classmethod
    def driver(cls, uri, auth):
        assert uri == "bolt://localhost:7687"
        assert auth == ("neo4j", "")
        cls.driver_instance = _FakeDriver()
        return cls.driver_instance


def _provider(**config):
    provider = KnowledgeGraphMemoryProvider()
    provider._available = True
    provider._session_id = "session-1"
    provider._cfg = {
        "capture_reasoning": False,
        "capture_tool_arguments": False,
        "capture_tool_results": False,
        **config,
    }
    provider._queue = _CaptureQueue()
    return provider


def test_sync_turn_uses_public_messages_contract_without_embedding_calls(
    monkeypatch,
):
    provider = _provider()

    def fail_if_called(_text):
        raise AssertionError(
            "sync_turn must not perform embedding network calls"
        )

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
                "content": "secret tool output",
            },
        ],
    )

    assert len(provider._queue.items) == 2
    turn = provider._queue.items[1]
    assert turn["type"] == "upsert"
    assert not any(
        node["kind"] == "reasoning" for node in turn["nodes"]
    )

    tool_node = next(
        node for node in turn["nodes"] if node["kind"] == "tool_call"
    )
    assert tool_node["props"]["name"] == "terminal"
    assert tool_node["props"]["arguments"].startswith("[redacted")

    result_node = next(
        node
        for node in turn["nodes"]
        if node["kind"] == "message"
        and node["props"]["role"] == "tool"
    )
    assert result_node["props"]["content"].startswith(
        "[tool result omitted"
    )
    assert "secret tool output" not in result_node["props"]["content"]
    assert any(
        rel["type"] == "PRODUCED" for rel in turn["relationships"]
    )


def test_sensitive_capture_requires_explicit_opt_in():
    provider = _provider(
        capture_reasoning=True,
        capture_tool_arguments=True,
        capture_tool_results=True,
    )
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
            {
                "role": "tool",
                "tool_call_id": "call-2",
                "content": "raw tool output",
            },
        ],
        "session-1",
    )

    reasoning = next(
        node for node in nodes if node["kind"] == "reasoning"
    )
    assert reasoning["props"]["content"] == "reasoning text"

    tool_node = next(
        node for node in nodes if node["kind"] == "tool_call"
    )
    assert "notes.md" in tool_node["props"]["arguments"]

    result_node = next(
        node
        for node in nodes
        if node["kind"] == "message"
        and node["props"]["role"] == "tool"
    )
    assert result_node["props"]["content"] == "raw tool output"
    assert any(rel["type"] == "REASONED" for rel in relationships)
    assert any(rel["type"] == "PRODUCED" for rel in relationships)


def test_repeated_identical_turns_receive_unique_message_ids():
    provider = _provider()
    messages = [
        {"role": "user", "content": "same question"},
        {"role": "assistant", "content": "same answer"},
    ]

    first_nodes, _ = provider._nodes_from_messages(
        messages,
        "session-1",
    )
    second_nodes, _ = provider._nodes_from_messages(
        messages,
        "session-1",
    )

    first_ids = {
        node["id"] for node in first_nodes if node["kind"] == "message"
    }
    second_ids = {
        node["id"] for node in second_nodes if node["kind"] == "message"
    }
    assert first_ids.isdisjoint(second_ids)


def test_quoted_false_config_values_remain_false(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "knowledge_graph": {
                "enabled": "false",
                "capture_reasoning": "false",
                "capture_tool_arguments": "false",
                "capture_tool_results": "false",
            }
        },
    )
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: tmp_path,
    )
    monkeypatch.delenv("HERMES_KG_ENABLED", raising=False)

    config = KnowledgeGraphMemoryProvider()._load_config()

    assert config["enabled"] is False
    assert config["capture_reasoning"] is False
    assert config["capture_tool_arguments"] is False
    assert config["capture_tool_results"] is False


def test_non_primary_context_is_read_only(
    monkeypatch,
    tmp_path,
):
    provider = KnowledgeGraphMemoryProvider()
    monkeypatch.setattr(
        provider,
        "_load_config",
        lambda: {
            "enabled": True,
            "uri": "bolt://localhost:7687",
            "user": "neo4j",
            "password": "",
            "database": "neo4j",
            "capture_reasoning": False,
            "capture_tool_arguments": False,
            "capture_tool_results": False,
            "embeddings_base_urls": [],
            "embeddings_model": "",
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "neo4j",
        SimpleNamespace(GraphDatabase=_FakeGraphDatabase),
    )

    provider.initialize(
        "subagent-session",
        hermes_home=str(tmp_path),
        platform="cli",
        agent_context="subagent",
    )

    assert provider._available is True
    assert provider._write_enabled is False
    assert provider._queue is None
    assert not (tmp_path / "knowledge_graph" / "pending.db").exists()

    schema_names = {
        schema["name"] for schema in provider.get_tool_schemas()
    }
    assert "kg_search" in schema_names
    assert "kg_query" in schema_names
    assert "kg_status" in schema_names
    assert "kg_remember" not in schema_names
    assert "kg_index_docs" not in schema_names
    assert "kg_forget" not in schema_names

    denied = json.loads(
        provider.handle_tool_call(
            "kg_remember",
            {"content": "must not be persisted"},
        )
    )
    assert denied["ok"] is False
    assert "read-only" in denied["error"]
    provider.shutdown()


def test_same_session_rewind_does_not_enqueue_lineage():
    provider = _provider()
    provider._prefetch["session-1"] = "stale recall"

    provider.on_session_switch(
        "session-1",
        parent_session_id="session-1",
        rewound=True,
        reason="rewind",
    )

    assert provider._queue.items == []
    assert "session-1" not in provider._prefetch


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
            "embeddings_base_urls": (
                "http://xwing:1234/v1,http://tie:1234/v1"
            ),
            "capture_tool_results": "false",
        },
        str(tmp_path),
    )

    config_path = tmp_path / "knowledge_graph.json"
    assert config_path.exists()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["enabled"] is True
    assert saved["capture_tool_results"] is False
    assert saved["embeddings_base_urls"] == [
        "http://xwing:1234/v1",
        "http://tie:1234/v1",
    ]


def test_is_available_is_configuration_only_without_network_call(
    monkeypatch,
):
    provider = KnowledgeGraphMemoryProvider()
    monkeypatch.setattr(
        provider,
        "_load_config",
        lambda: {"enabled": True, "uri": "bolt://localhost:7687"},
    )
    monkeypatch.setattr(
        "plugins.memory.knowledge_graph.provider.importlib.util.find_spec",
        lambda name: object() if name == "neo4j" else None,
    )

    assert provider.is_available() is True


def test_durable_queue_deletes_rows_only_after_success(tmp_path):
    observed = []
    queue = _DurableQueue(tmp_path / "queue.db", observed.append)
    queue.enqueue(
        {"type": "upsert", "nodes": [], "relationships": []}
    )

    deadline = time.time() + 3
    while queue.pending_count() and time.time() < deadline:
        time.sleep(0.02)

    assert observed == [
        {"type": "upsert", "nodes": [], "relationships": []}
    ]
    assert queue.pending_count() == 0
    queue.close()


def test_document_chunks_overlap_without_infinite_loop():
    provider = KnowledgeGraphMemoryProvider()
    text = " ".join(f"token-{index}" for index in range(1000))
    chunks = list(provider._chunks(text, size=300, overlap=50))

    assert len(chunks) > 1
    assert all(chunks)
    assert sum(len(chunk) for chunk in chunks) > len(text)
