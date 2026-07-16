"""Unit tests for the finetuning exporter (DB-independent; uses a synthetic db)."""

from __future__ import annotations

import json

from plugins.memory.knowledge_graph.finetune_export import (
    ExportConfig,
    export_finetune,
    build_export_config,
)


def _make_db(path: str) -> None:
    """Mirror helper from test_opencode_import; build a tiny opencode.db."""
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, title TEXT, directory TEXT,
            model TEXT, agent TEXT, cost REAL, tokens_input INT,
            tokens_output INT, tokens_reasoning INT, time_created INT,
            time_updated INT
        );
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created INT, time_updated INT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INT, time_updated INT, data TEXT);
        """
    )
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_1", "prj", "Auth refactor", "/repo",
         '{"id":"hy3","providerID":"openrouter"}', "build", 0.01, 100, 50, 20, 1000, 1000),
    )
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("msg_1", "ses_1", 1001, 1001, json.dumps({"role": "user"})),
    )
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("prt_1", "msg_1", "ses_1", 1001, 1001, json.dumps({"type": "text", "text": "reverse a list"})),
    )
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("msg_2", "ses_1", 1002, 1002, json.dumps({"role": "assistant"})),
    )
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("prt_2", "msg_2", "ses_1", 1002, 1002,
         json.dumps({"type": "reasoning", "text": "Slicing is idiomatic."})),
    )
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("prt_3", "msg_2", "ses_1", 1003, 1003, json.dumps({"type": "text", "text": "Use lst[::-1]."})),
    )
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("prt_4", "msg_2", "ses_1", 1004, 1004,
         json.dumps({"type": "tool", "tool": "python_exec", "callID": "c1",
                     "state": {"status": "completed", "input": {"code": "[1,2,3][::-1]"},
                               "output": "[3, 2, 1]"}})),
    )
    # A degenerate session (single user msg, no assistant) -> should be skipped.
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_2", "prj", "Empty", "/x", '{"id":"m","providerID":"p"}', "build",
         0, 0, 0, 0, 2000, 2000),
    )
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("msg_9", "ses_2", 2001, 2001, json.dumps({"role": "user"})),
    )
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("prt_9", "msg_9", "ses_2", 2001, 2001, json.dumps({"type": "text", "text": "hi"})),
    )
    con.commit()
    con.close()


def test_export_openai_format_includes_reasoning(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    out = tmp_path / "out.jsonl"
    res = export_finetune(str(db), str(out), config=ExportConfig(format="openai",
                                                                include_reasoning=True))
    assert res["examples"] == 1, res  # ses_2 skipped as degenerate
    assert res["skipped"] == 1
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    roles = [m["role"] for m in rec["messages"]]
    assert roles == ["system", "user", "assistant"]
    user = rec["messages"][1]
    assistant = rec["messages"][2]
    assert user["content"] == "reverse a list"
    assert assistant["content"] == "Use lst[::-1]."
    assert assistant["reasoning_content"] == "Slicing is idiomatic."


def test_export_excludes_reasoning_when_disabled(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    out = tmp_path / "out.jsonl"
    res = export_finetune(str(db), str(out), config=ExportConfig(format="openai",
                                                                include_reasoning=False))
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assistant = rec["messages"][2]
    assert "reasoning_content" not in assistant


def test_export_sharegpt_format(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    out = tmp_path / "out.jsonl"
    res = export_finetune(str(db), str(out), config=ExportConfig(format="sharegpt"))
    rec = json.loads(out.read_text().strip().splitlines()[0])
    assert "conversations" in rec
    fr = [c["from"] for c in rec["conversations"]]
    assert fr == ["system", "human", "gpt"]
    assert rec["metadata"]["source"] == "opencode"
    assert rec["metadata"]["model"] == "openrouter/hy3"


def test_export_tool_io_off_by_default(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    out = tmp_path / "out.jsonl"
    export_finetune(str(db), str(out), config=ExportConfig(format="openai"))
    rec = json.loads(out.read_text().strip().splitlines()[0])
    # tool call/result NOT present when include_tool_io is False.
    assert not any(t["role"].startswith("tool") for t in rec["messages"])


def test_export_tool_io_included(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    out = tmp_path / "out.jsonl"
    export_finetune(str(db), str(out),
                    config=ExportConfig(format="openai", include_tool_io=True))
    rec = json.loads(out.read_text().strip().splitlines()[0])
    roles = [m["role"] for m in rec["messages"]]
    # tool call content + tool result output appear.
    contents = [m.get("content", "") for m in rec["messages"]]
    assert any("python_exec" in c for c in contents)
    assert any("[3, 2, 1]" in c for c in contents)
    assert "tool" in roles or "tool_result" in roles


def test_export_dry_run_writes_nothing(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    out = tmp_path / "out.jsonl"
    res = export_finetune(str(db), str(out), config=ExportConfig(), dry_run=True)
    assert res["dry_run"] is True
    assert not out.exists()


def test_build_export_config_helper():
    cfg = build_export_config(format="sharegpt", include_reasoning=False,
                              min_messages=4)
    assert cfg.format == "sharegpt"
    assert cfg.include_reasoning is False
    assert cfg.min_messages == 4


def test_export_max_examples_limits(tmp_path):
    db = tmp_path / "oc.db"
    _make_db(str(db))
    out = tmp_path / "out.jsonl"
    # Only ses_1 is valid, so max_examples=1 still yields 1.
    res = export_finetune(str(db), str(out),
                          config=ExportConfig(max_examples=1))
    assert res["examples"] == 1
