from __future__ import annotations

import json
import sqlite3
from unittest import mock

from hermes_state import SCHEMA_SQL
from plugins.memory.knowledge_graph import KnowledgeGraphMemoryProvider
from plugins.memory.knowledge_graph.session_backfill import build_graph, import_state_db


def _init_state_db(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    con.execute(
        """
        INSERT INTO sessions (
            id, source, user_id, model, model_config, system_prompt,
            parent_session_id, started_at, ended_at, end_reason, title
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("sess-a", "cli", "u1", "gpt-test", "{}", "sys", "parent-a",
         100.0, 200.0, "exit", "Backfill test"),
    )
    tool_calls = [{
        "id": "call_1",
        "function": {"name": "terminal", "arguments": "{\"command\": \"date\"}"},
    }]
    con.execute(
        """
        INSERT INTO messages (
            session_id, role, content, tool_call_id, tool_calls, tool_name,
            timestamp, token_count, finish_reason, reasoning, reasoning_content,
            reasoning_details, codex_reasoning_items, codex_message_items,
            platform_message_id, observed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("sess-a", "user", "hello", None, None, None, 101.0, 3, None,
         None, None, None, None, None, "pm1", 0),
    )
    con.execute(
        """
        INSERT INTO messages (
            session_id, role, content, tool_call_id, tool_calls, tool_name,
            timestamp, token_count, finish_reason, reasoning, reasoning_content,
            reasoning_details, codex_reasoning_items, codex_message_items,
            platform_message_id, observed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("sess-a", "assistant", "running date", None, json.dumps(tool_calls),
         None, 102.0, 5, "tool_calls", "think", None, None, None, None,
         "pm2", 0),
    )
    con.execute(
        """
        INSERT INTO messages (
            session_id, role, content, tool_call_id, tool_calls, tool_name,
            timestamp, token_count, finish_reason, reasoning, reasoning_content,
            reasoning_details, codex_reasoning_items, codex_message_items,
            platform_message_id, observed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("sess-a", "tool", "Wed Jul 15", "call_1", None, "terminal",
         103.0, 2, None, None, None, None, None, None, "pm3", 0),
    )
    con.commit()
    con.close()


def test_state_backfill_builds_stable_graph(tmp_path):
    db = tmp_path / "state.db"
    _init_state_db(db)

    graph1 = build_graph(str(db))
    graph2 = build_graph(str(db))

    assert [n["id"] for n in graph1["nodes"]] == [n["id"] for n in graph2["nodes"]]
    assert graph1["counts"] == {
        "sessions": 1, "messages": 2, "reasoning": 1,
        "toolcalls": 1, "toolresults": 1,
    }
    ids = {n["id"] for n in graph1["nodes"]}
    assert "sess:sess-a" in ids
    assert "sess:parent-a" in ids
    assert any(i.startswith("msg:hermes:") for i in ids)
    assert any(i.startswith("reasoning:hermes:") for i in ids)
    assert any(i.startswith("toolcall:hermes:") for i in ids)
    assert any(i.startswith("toolresult:hermes:") for i in ids)
    rels = {(r["src"], r["rel"], r["dst"]) for r in graph1["rels"]}
    assert ("sess:parent-a", "DERIVED_TO", "sess:sess-a") in rels
    assert any(r[1] == "FOLLOWED_BY" for r in rels)
    assert any(r[1] == "REASONED" for r in rels)
    assert any(r[1] == "CALLED" for r in rels)
    assert any(r[1] == "PRODUCED" for r in rels)


def test_state_backfill_dry_run_and_bulk_write(tmp_path):
    db = tmp_path / "state.db"
    _init_state_db(db)
    store = mock.MagicMock()
    store.bulk_merge_nodes.return_value = 6
    store.bulk_merge_relationships.return_value = 7
    embed = mock.MagicMock()
    embed.dimension = 3
    embed.embed_many.return_value = [[0.1, 0.2, 0.3]] * 3

    dry = import_state_db(store, embed, str(db), dry_run=True)
    assert dry["dry_run"] is True
    assert dry["to_embed"] == 3  # 2 messages + 1 reasoning; tools are graph-only.
    store.bulk_merge_nodes.assert_not_called()

    out = import_state_db(store, embed, str(db))
    assert out["dry_run"] is False
    assert out["embedded"] == 3
    store.ensure_schema.assert_called_with(3)
    node_rows = store.bulk_merge_nodes.call_args[0][0]
    assert any(row["id"] == "sess:sess-a" and row["labels"] == ["KgSession"]
               for row in node_rows)


def test_kg_provider_session_switch_enqueues_metadata():
    p = KnowledgeGraphMemoryProvider()
    p._cfg = {"capture": {"sessions": True}}
    p._available = True
    p._session_id = "old-session"
    p._platform = "cli"
    p._profile = "default"
    p._model = "gpt-test"
    p._enqueue_job = mock.MagicMock()  # type: ignore[method-assign]

    p.on_session_switch("new-session", parent_session_id="old-session", event="branch")

    assert p._session_id == "new-session"
    sid, job = p._enqueue_job.call_args[0]
    assert sid == "new-session"
    assert job["type"] == "session"
    assert job["parent_session_id"] == "old-session"
    assert job["event"] == "branch"
    assert job["model"] == "gpt-test"



def test_kg_import_sessions_tool_defaults_to_dry_run(tmp_path):
    db = tmp_path / "state.db"
    _init_state_db(db)
    p = KnowledgeGraphMemoryProvider()
    p._cfg = {"capture": {"sessions": True}}
    p._available = True
    p._store = mock.MagicMock()
    p._embed = mock.MagicMock()
    p._embed.dimension = 3

    out = p._dispatch("kg_import_sessions", {"db_path": str(db)})

    assert out["dry_run"] is True
    assert out["counts"]["sessions"] == 1
    assert out["db_path"] == str(db)
    p._store.bulk_merge_nodes.assert_not_called()

