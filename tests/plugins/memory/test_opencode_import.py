"""Unit tests for the OpenCode -> knowledge-graph importer.

Builds a tiny synthetic ``opencode.db`` (the same three-table shape the real
database uses) and asserts the importer maps sessions/messages/parts onto the
knowledge-graph node + edge model, embeds the right subset, and is idempotent.
No Neo4j or embeddings endpoint is touched — the store and embedder are mocked.
"""

from __future__ import annotations

import json
import sqlite3
from unittest import mock

from plugins.memory.knowledge_graph.opencode_import import (
    build_graph,
    import_opencode,
    _parse_model,
)


def _make_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT,
            slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT,
            share_url TEXT, summary_additions INT, summary_deletions INT,
            summary_files INT, summary_diffs TEXT, metadata TEXT, cost REAL,
            tokens_input INT, tokens_output INT, tokens_reasoning INT,
            tokens_cache_read INT, tokens_cache_write INT, revert TEXT,
            permission TEXT, agent TEXT, model TEXT, time_created INT,
            time_updated INT, time_compacting INT, time_archived INT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INT,
            time_updated INT, data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INT, time_updated INT, data TEXT
        );
        """
    )

    def sess(id, title, directory, model, agent, t):
        con.execute(
            "INSERT INTO session (id,title,directory,model,agent,cost,"
            "tokens_input,tokens_output,tokens_reasoning,time_created,time_updated,"
            "project_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (id, title, directory, model, agent, 0.01, 100, 50, 20, t, t, "prj_1"),
        )

    def msg(id, sid, role, t):
        con.execute(
            "INSERT INTO message (id,session_id,time_created,time_updated,data) "
            "VALUES (?,?,?,?,?)",
            (id, sid, t, t, json.dumps({"role": role, "modelID": "m1"})),
        )

    def part(id, mid, sid, t, data):
        con.execute(
            "INSERT INTO part (id,message_id,session_id,time_created,time_updated,data) "
            "VALUES (?,?,?,?,?,?)",
            (id, mid, sid, t, t, json.dumps(data)),
        )

    sess("ses_1", "Refactor auth", "/repo/app",
         json.dumps({"id": "hy3", "providerID": "openrouter"}), "build", 1000)
    # user turn
    msg("msg_1", "ses_1", "user", 1001)
    part("prt_1", "msg_1", "ses_1", 1001, {"type": "text", "text": "reverse a list"})
    # assistant turn: reasoning + text + tool call w/ output
    msg("msg_2", "ses_1", "assistant", 1002)
    part("prt_2", "msg_2", "ses_1", 1002,
         {"type": "reasoning", "text": "Slicing lst[::-1] is idiomatic."})
    part("prt_3", "msg_2", "ses_1", 1003, {"type": "text", "text": "Use lst[::-1]."})
    part("prt_4", "msg_2", "ses_1", 1004,
         {"type": "tool", "tool": "python_exec", "callID": "c1",
          "state": {"status": "completed", "input": {"code": "[1,2,3][::-1]"},
                    "output": "[3, 2, 1]"}})
    part("prt_5", "msg_2", "ses_1", 1005, {"type": "step-finish", "reason": "stop"})

    con.commit()
    con.close()


def test_parse_model_variants():
    assert _parse_model(json.dumps({"id": "gpt-5", "providerID": "openai"})) == "openai/gpt-5"
    assert _parse_model('{"id":"m"}') == "m"
    assert _parse_model("plain-string") == "plain-string"
    assert _parse_model("") == ""


def test_build_graph_maps_sessions_messages_parts(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    g = build_graph(str(db))

    assert g["sessions"] == 1
    counts = g["counts"]
    assert counts["messages"] == 2
    assert counts["reasoning"] == 1
    assert counts["toolcalls"] == 1
    assert counts["toolresults"] == 1

    by_id = {n["id"]: n for n in g["nodes"]}
    # Stable, source-tagged ids.
    assert "sess:oc:ses_1" in by_id
    assert "msg:oc:msg_1" in by_id
    assert "msg:oc:msg_2" in by_id
    assert "reasoning:oc:prt_2" in by_id
    assert "toolcall:oc:prt_4" in by_id
    assert "toolresult:oc:prt_4" in by_id

    sess = by_id["sess:oc:ses_1"]
    assert sess["kind"] == "session"
    assert "KgSession" in sess["labels"]
    assert sess["props"]["source"] == "opencode"
    assert sess["props"]["model"] == "openrouter/hy3"
    assert sess["props"]["directory"] == "/repo/app"

    # Assistant message content is assembled from its text parts.
    assert by_id["msg:oc:msg_2"]["props"]["content"] == "Use lst[::-1]."
    # Reasoning captured verbatim.
    assert "Slicing" in by_id["reasoning:oc:prt_2"]["props"]["content"]
    # Tool result carries the output.
    assert by_id["toolresult:oc:prt_4"]["props"]["content"] == "[3, 2, 1]"


def test_build_graph_edges(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    g = build_graph(str(db))
    edges = {(r["src"], r["rel"], r["dst"]) for r in g["rels"]}

    assert ("sess:oc:ses_1", "HAS", "msg:oc:msg_1") in edges
    assert ("sess:oc:ses_1", "HAS", "reasoning:oc:prt_2") in edges
    assert ("sess:oc:ses_1", "HAS", "toolcall:oc:prt_4") in edges
    # user -> assistant ordering
    assert ("msg:oc:msg_1", "FOLLOWED_BY", "msg:oc:msg_2") in edges
    # assistant reasoned + called + produced
    assert ("msg:oc:msg_2", "REASONED", "reasoning:oc:prt_2") in edges
    assert ("msg:oc:msg_2", "CALLED", "toolcall:oc:prt_4") in edges
    assert ("toolcall:oc:prt_4", "PRODUCED", "toolresult:oc:prt_4") in edges


def test_embed_scope_default_is_cot_and_messages_only(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    g = build_graph(str(db))
    embed_ids = g["embed_ids"]
    # Messages (with content) + reasoning are embedded.
    assert "msg:oc:msg_1" in embed_ids
    assert "msg:oc:msg_2" in embed_ids
    assert "reasoning:oc:prt_2" in embed_ids
    # Tool call / result are graph-only by default.
    assert "toolcall:oc:prt_4" not in embed_ids
    assert "toolresult:oc:prt_4" not in embed_ids


def test_embed_scope_with_embed_tools(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    g = build_graph(str(db), embed_tools=True)
    embed_ids = g["embed_ids"]
    assert "toolcall:oc:prt_4" in embed_ids
    assert "toolresult:oc:prt_4" in embed_ids


def test_import_writes_nodes_and_embeds_selected(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    store = mock.MagicMock()
    embed = mock.MagicMock()
    embed.dimension = 8
    embed.embed.return_value = [0.1] * 8
    embed.embed_many.side_effect = lambda texts: [[0.1] * 8 for _ in texts]

    out = import_opencode(store, embed, str(db))
    assert out["dry_run"] is False
    assert out["counts"]["sessions"] == 1

    # bulk_merge_nodes called with the full node set.
    assert store.bulk_merge_nodes.called
    node_rows = store.bulk_merge_nodes.call_args.args[0]
    row_ids = {r["id"] for r in node_rows}
    assert "sess:oc:ses_1" in row_ids
    assert "reasoning:oc:prt_2" in row_ids
    # session node carries the KgSession label.
    sess_row = next(r for r in node_rows if r["id"] == "sess:oc:ses_1")
    assert "KgSession" in sess_row["labels"]

    # Only message + reasoning texts were embedded (msg_1, msg_2, reasoning = 3).
    embedded_texts = embed.embed_many.call_args.args[0]
    assert len(embedded_texts) == out["embedded"] == 3

    # Relationships written via bulk call.
    assert store.bulk_merge_relationships.called
    rel_rows = store.bulk_merge_relationships.call_args.args[0]
    assert len(rel_rows) == len(build_graph(str(db))["rels"])


def test_import_is_idempotent_on_ids(tmp_path):
    # Same ids on a second run -> MERGE-friendly (store gets identical ids).
    db = tmp_path / "oc.db"
    _make_db(str(db))
    g1 = build_graph(str(db))
    g2 = build_graph(str(db))
    ids1 = sorted(n["id"] for n in g1["nodes"])
    ids2 = sorted(n["id"] for n in g2["nodes"])
    assert ids1 == ids2


def test_dry_run_does_not_write(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    store = mock.MagicMock()
    out = import_opencode(store, None, str(db), dry_run=True)
    assert out["dry_run"] is True
    store.merge_node.assert_not_called()
    store.merge_relationship.assert_not_called()
