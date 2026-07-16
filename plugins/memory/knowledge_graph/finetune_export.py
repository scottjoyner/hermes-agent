"""Export OpenCode / knowledge-graph conversations for finetuning.

This produces training examples (SFT pairs) from chat histories sourced from
``opencode.db`` (via :func:`build_graph`). It is deliberately DB-independent:
it only reads the local SQLite file (opened read-only) and writes a JSONL file
— no Neo4j, no embeddings endpoint required.

Two formats are supported:

* ``openai``  — one line per example:
    {"messages": [{"role":"system","content":...},
                  {"role":"user","content":...},
                  {"role":"assistant","content":..., "reasoning_content":...}]}
* ``sharegpt`` — one line per example:
    {"conversations": [{"from":"system"/"human"/"gpt","value":...}],
     "metadata": {...}}

Quality filters drop degenerate examples (too-short sessions, empty turns,
compaction/scratch sessions) so the resulting corpus is usable for SFT.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from plugins.memory.knowledge_graph.opencode_import import build_graph, _parse_model


# Short system prompt describing the model's role, derived per session when
# available. Keeps the corpus self-describing without leaking private context.
_SYSTEM_TEMPLATE = (
    "You are an autonomous coding agent working in the directory '{directory}'. "
    "Use tools to explore, edit, run, and test code. Think step by step."
)


@dataclass
class ExportConfig:
    format: str = "openai"            # "openai" | "sharegpt"
    include_reasoning: bool = True    # surface CoT as assistant.reasoning_content
    min_messages: int = 1             # drop sessions with fewer than this many turns
    include_tool_io: bool = False     # include tool call/result turns in the flow
    max_examples: Optional[int] = None
    source: str = "opencode"


def _session_blocks(graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Bucket nodes by session id; return {session_id: {meta, msgs, cot, tools}}."""
    sessions: Dict[str, Dict[str, Any]] = {}
    node_by_id = {n["id"]: n for n in graph["nodes"]}

    # Session meta.
    for n in graph["nodes"]:
        if n["kind"] == "session":
            sessions[n["props"]["session_id"]] = {
                "meta": n["props"], "messages": [], "cot": {}, "tools": {},
            }

    # Messages (ordered by seq / created timestamp) — sort via stored order.
    msgs = [n for n in graph["nodes"] if n["kind"] == "message"]
    msgs.sort(key=lambda n: (n["props"].get("created_at_ms") or 0, n["id"]))
    for m in msgs:
        sid = m["props"].get("session_id")
        if sid in sessions:
            sessions[sid]["messages"].append(m)

    # Reasoning keyed by parent message id (REASONED edges).
    cot_by_msg: Dict[str, List[str]] = {}
    for r in graph["rels"]:
        if r["rel"] == "REASONED":
            cot_by_msg.setdefault(r["src"], []).append(r["dst"])
    # Tool call/result keyed by parent message id (CALLED / PRODUCED).
    tools_by_msg: Dict[str, List[Dict[str, str]]] = {}
    # Map each toolcall node to the message that CALLED it, so PRODUCED
    # (toolcall -> toolresult) can be attached to the right message.
    call_to_msg: Dict[str, str] = {}
    for r in graph["rels"]:
        if r["rel"] == "CALLED":
            tools_by_msg.setdefault(r["src"], []).append(
                {"call": r["dst"], "result": None})
            call_to_msg[r["dst"]] = r["src"]
        elif r["rel"] == "PRODUCED":
            # Attach the result to the call's owning message entry.
            msg_id = call_to_msg.get(r["src"])
            if msg_id:
                for entry in reversed(tools_by_msg.get(msg_id, [])):
                    if entry["result"] is None and entry["call"] == r["src"]:
                        entry["result"] = r["dst"]
                        break

    for sid, blk in sessions.items():
        for m in blk["messages"]:
            mid = m["id"]
            blk["cot"][mid] = [node_by_id[c]["props"].get("content", "")
                              for c in cot_by_msg.get(mid, [])
                              if c in node_by_id]
            blk["tools"][mid] = tools_by_msg.get(mid, [])
    return sessions


def _build_messages(
    blk: Dict[str, Any], cfg: ExportConfig, node_by_id: Dict[str, Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    meta = blk["meta"]
    turns: List[Dict[str, Any]] = []

    # System context from session metadata.
    directory = meta.get("directory") or ""
    turns.append({"role": "system",
                  "content": _SYSTEM_TEMPLATE.format(directory=directory)})

    for m in blk["messages"]:
        role = m["props"].get("role")
        content = (m["props"].get("content") or "").strip()
        if role == "user":
            if content:
                turns.append({"role": "user", "content": content})
        elif role == "assistant":
            if not content and not cfg.include_reasoning:
                continue
            entry: Dict[str, Any] = {"role": "assistant", "content": content}
            if cfg.include_reasoning:
                cot = "\n\n".join(c for c in blk["cot"].get(m["id"], []) if c).strip()
                if cot:
                    entry["reasoning_content"] = cot
            turns.append(entry)
            if cfg.include_tool_io:
                for t in blk["tools"].get(m["id"], []):
                    call_node = node_by_id.get(t["call"]) if t["call"] else None
                    res_node = node_by_id.get(t["result"]) if t["result"] else None
                    if call_node:
                        turns.append({"role": "tool",
                                      "content": call_node["props"].get("content", "")})
                    if res_node:
                        turns.append({"role": "tool_result",
                                      "content": res_node["props"].get("content", "")})
        # bare tool_result messages (no assistant context) — only with flag
        elif role == "tool_result" and cfg.include_tool_io:
            if content:
                turns.append({"role": "tool_result", "content": content})

    # Quality filter: enough real user/assistant conversation.
    real = [t for t in turns if t["role"] in ("user", "assistant")]
    if len(real) < cfg.min_messages:
        return None
    # Drop sessions that are basically empty.
    assistant_text = [t for t in turns if t["role"] == "assistant"
                      and (t.get("content") or t.get("reasoning_content"))]
    if not assistant_text:
        return None
    return turns


def export_finetune(
    db_path: str,
    out_path: str,
    *,
    config: Optional[ExportConfig] = None,
    since_ms: Optional[int] = None,
    limit_sessions: Optional[int] = None,
    embed_tools: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Build finetuning examples from opencode.db and write JSONL.

    Returns a summary dict (``examples``, ``sessions_seen``, ``skipped``, ...).
    """
    cfg = config or ExportConfig()
    graph = build_graph(db_path, since_ms=since_ms,
                        limit_sessions=limit_sessions, embed_tools=embed_tools)
    node_by_id = {n["id"]: n for n in graph["nodes"]}

    sessions = _session_blocks(graph)
    examples: List[Dict[str, Any]] = []
    skipped = 0
    for sid, blk in sessions.items():
        msgs = _build_messages(blk, cfg, node_by_id)
        if msgs is None:
            skipped += 1
            continue
        if cfg.format == "openai":
            rec = {"messages": msgs}
        elif cfg.format == "sharegpt":
            conv = []
            role_map = {"system": "system", "user": "human", "assistant": "gpt",
                        "tool": "function", "tool_result": "function"}
            for t in msgs:
                conv.append({"from": role_map.get(t["role"], "human"),
                             "value": t.get("content") or ""})
            rec = {"conversations": conv, "metadata": {"source": cfg.source,
                                                       "session_id": sid,
                                                       "model": blk["meta"].get("model", ""),
                                                       "directory": blk["meta"].get("directory", "")}}
        else:
            raise ValueError(f"unknown format: {cfg.format}")
        examples.append(rec)
        if cfg.max_examples and len(examples) >= cfg.max_examples:
            break

    if dry_run:
        return {"dry_run": True, "sessions_seen": len(sessions),
                "examples": len(examples), "skipped": skipped}

    with open(out_path, "w", encoding="utf-8") as fh:
        for ex in examples:
            fh.write(json.dumps(ex, ensure_ascii=False) + "\n")

    return {"dry_run": False, "sessions_seen": len(sessions),
            "examples": len(examples), "skipped": skipped, "out": out_path,
            "format": cfg.format}


def build_export_config(
    format: str = "openai",
    include_reasoning: bool = True,
    include_tool_io: bool = False,
    min_messages: int = 2,
    max_examples: Optional[int] = None,
) -> ExportConfig:
    return ExportConfig(format=format, include_reasoning=include_reasoning,
                        include_tool_io=include_tool_io, min_messages=min_messages,
                        max_examples=max_examples)
