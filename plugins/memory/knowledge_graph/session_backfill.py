"""Backfill Hermes ``state.db`` sessions into the knowledge graph.

Hermes' primary session store is SQLite (``state.db``). Live knowledge-graph
capture writes turns as they happen, but older sessions and missed writes need a
safe reconciliation path. This module reads ``state.db`` read-only and maps rows
to the same graph shape as live capture using stable ids derived from SQLite
primary keys:

    session   -> sess:<session_id>
    message   -> msg:hermes:<message_row_id>
    reasoning -> reasoning:hermes:<message_row_id>:<field>
    toolcall  -> toolcall:hermes:<message_row_id>:<tool_call_id|hash>
    result    -> toolresult:hermes:<message_row_id>

The import is idempotent: re-running MERGEs the same node ids and relationships.
By default, messages + reasoning are embedded. Tool call/result text remains
reachable by graph traversal and can be embedded with ``embed_tools=True``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Set

_PROVENANCE = "hermes_state_db"


def _hash8(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:8]


def _node(node_id: str, kind: str, props: Dict[str, Any],
          labels: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"id": node_id, "kind": kind, "props": props, "labels": labels or []}


def _rel(src: str, rel: str, dst: str) -> Dict[str, Any]:
    return {"src": src, "rel": rel, "dst": dst, "props": {}}


def _open_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _jsonish(raw: Any, limit: int = 4000) -> str:
    if raw in (None, ""):
        return ""
    if isinstance(raw, str):
        return raw[:limit]
    try:
        return json.dumps(raw, ensure_ascii=False)[:limit]
    except Exception:
        return str(raw)[:limit]


def _session_props(row: sqlite3.Row) -> Dict[str, Any]:
    keys = set(row.keys())
    def g(name: str, default: Any = "") -> Any:
        return row[name] if name in keys and row[name] is not None else default

    return {
        "session_id": row["id"],
        "source": _PROVENANCE,
        "session_source": g("source"),
        "user_id": g("user_id"),
        "model": g("model"),
        "model_config": g("model_config"),
        "parent_session_id": g("parent_session_id"),
        "started_at": g("started_at", 0),
        "ended_at": g("ended_at", 0),
        "end_reason": g("end_reason"),
        "message_count": g("message_count", 0),
        "tool_call_count": g("tool_call_count", 0),
        "input_tokens": g("input_tokens", 0),
        "output_tokens": g("output_tokens", 0),
        "reasoning_tokens": g("reasoning_tokens", 0),
        "title": g("title"),
        "content": g("title") or row["id"],
        "cost_status": g("cost_status"),
        "estimated_cost_usd": g("estimated_cost_usd", 0),
        "actual_cost_usd": g("actual_cost_usd", 0),
    }


def build_graph(
    db_path: str,
    *,
    since_ts: Optional[float] = None,
    limit_sessions: Optional[int] = None,
    session_ids: Optional[List[str]] = None,
    embed_tools: bool = False,
) -> Dict[str, Any]:
    """Build KG rows from Hermes ``state.db`` without writing anything."""
    con = _open_ro(db_path)
    cur = con.cursor()

    clauses: List[str] = []
    params: List[Any] = []
    if since_ts is not None:
        clauses.append("started_at >= ?")
        params.append(float(since_ts))
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        clauses.append(f"id IN ({placeholders})")
        params.extend(session_ids)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    limit = "LIMIT ?" if limit_sessions else ""
    if limit_sessions:
        params.append(int(limit_sessions))

    sessions = cur.execute(
        f"SELECT * FROM sessions {where} ORDER BY started_at ASC, id ASC {limit}",
        params,
    ).fetchall()

    nodes: List[Dict[str, Any]] = []
    rels: List[Dict[str, Any]] = []
    embed_ids: Set[str] = set()
    counts = {"sessions": 0, "messages": 0, "reasoning": 0,
              "toolcalls": 0, "toolresults": 0}

    for srow in sessions:
        sid = srow["id"]
        sess_node = f"sess:{sid}"
        sprops = _session_props(srow)
        nodes.append(_node(sess_node, "session", sprops, labels=["KgSession"]))
        counts["sessions"] += 1
        parent = sprops.get("parent_session_id") or ""
        if parent and parent != sid:
            parent_node = f"sess:{parent}"
            nodes.append(_node(parent_node, "session", {"session_id": parent},
                               labels=["KgSession"]))
            rels.append(_rel(parent_node, "DERIVED_TO", sess_node))

        messages = cur.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp ASC, id ASC",
            (sid,),
        ).fetchall()
        prev_msg: Optional[str] = None
        toolcall_by_call_id: Dict[str, str] = {}

        for mrow in messages:
            mid = int(mrow["id"])
            role = mrow["role"] or ""
            content = mrow["content"] or ""
            ts = mrow["timestamp"] or 0

            if role == "tool":
                nid = f"toolresult:hermes:{mid}"
                nodes.append(_node(nid, "toolresult", {
                    "role": "tool_result", "content": content, "session_id": sid,
                    "source": _PROVENANCE, "sqlite_message_id": mid,
                    "tool_call_id": mrow["tool_call_id"] or "",
                    "tool_name": mrow["tool_name"] or "", "timestamp": ts,
                }))
                counts["toolresults"] += 1
                if embed_tools and content:
                    embed_ids.add(nid)
                rels.append(_rel(sess_node, "HAS", nid))
                call_id = mrow["tool_call_id"] or ""
                if call_id and call_id in toolcall_by_call_id:
                    rels.append(_rel(toolcall_by_call_id[call_id], "PRODUCED", nid))
            else:
                nid = f"msg:hermes:{mid}"
                nodes.append(_node(nid, "message", {
                    "role": role, "content": content, "session_id": sid,
                    "source": _PROVENANCE, "sqlite_message_id": mid,
                    "token_count": mrow["token_count"] or 0,
                    "finish_reason": mrow["finish_reason"] or "",
                    "platform_message_id": mrow["platform_message_id"] or "",
                    "observed": bool(mrow["observed"]), "timestamp": ts,
                }))
                counts["messages"] += 1
                if content:
                    embed_ids.add(nid)
                rels.append(_rel(sess_node, "HAS", nid))
                if prev_msg:
                    rels.append(_rel(prev_msg, "FOLLOWED_BY", nid))
                prev_msg = nid

                reasoning_fields = {
                    "reasoning": mrow["reasoning"],
                    "reasoning_content": mrow["reasoning_content"],
                    "reasoning_details": mrow["reasoning_details"],
                    "codex_reasoning_items": mrow["codex_reasoning_items"],
                }
                for field, raw in reasoning_fields.items():
                    rtext = _jsonish(raw, limit=12000).strip()
                    if not rtext:
                        continue
                    rid = f"reasoning:hermes:{mid}:{field}"
                    nodes.append(_node(rid, "reasoning", {
                        "content": rtext, "field": field, "session_id": sid,
                        "source": _PROVENANCE, "sqlite_message_id": mid,
                    }))
                    embed_ids.add(rid)
                    counts["reasoning"] += 1
                    rels.append(_rel(nid, "REASONED", rid))
                    rels.append(_rel(sess_node, "HAS", rid))

                for idx, tc in enumerate(_loads(mrow["tool_calls"], [])):
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    name = tc.get("name") or fn.get("name") or ""
                    args = tc.get("arguments") or fn.get("arguments") or ""
                    call_id = tc.get("id") or tc.get("tool_call_id") or f"{mid}:{idx}:{_hash8(name + _jsonish(args))}"
                    tid = f"toolcall:hermes:{mid}:{_hash8(str(call_id))}"
                    arg_str = _jsonish(args)
                    nodes.append(_node(tid, "toolcall", {
                        "name": name, "arguments": arg_str,
                        "content": f"{name}({arg_str})"[:4000],
                        "session_id": sid, "source": _PROVENANCE,
                        "sqlite_message_id": mid, "tool_call_id": str(call_id),
                    }))
                    counts["toolcalls"] += 1
                    if embed_tools:
                        embed_ids.add(tid)
                    rels.append(_rel(nid, "CALLED", tid))
                    rels.append(_rel(sess_node, "HAS", tid))
                    toolcall_by_call_id[str(call_id)] = tid

    con.close()
    # The same parent placeholder can be added many times; Neo4j MERGE handles it.
    return {"nodes": nodes, "rels": rels, "embed_ids": embed_ids,
            "sessions": counts["sessions"], "counts": counts}


def import_state_db(
    store,
    embed,
    db_path: str,
    *,
    since_ts: Optional[float] = None,
    limit_sessions: Optional[int] = None,
    session_ids: Optional[List[str]] = None,
    embed_tools: bool = False,
    dry_run: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Import Hermes ``state.db`` sessions into Neo4j via ``store``/``embed``."""
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    graph = build_graph(
        db_path,
        since_ts=since_ts,
        limit_sessions=limit_sessions,
        session_ids=session_ids,
        embed_tools=embed_tools,
    )
    nodes = graph["nodes"]
    rels = graph["rels"]
    embed_ids = graph["embed_ids"]
    counts = graph["counts"]
    say(f"parsed {counts['sessions']} sessions, {counts['messages']} messages, "
        f"{counts['reasoning']} reasoning, {counts['toolcalls']} tool calls "
        f"({len(embed_ids)} nodes to embed)")

    if dry_run:
        return {"dry_run": True, "counts": counts, "nodes": len(nodes),
                "rels": len(rels), "to_embed": len(embed_ids)}

    vec_map: Dict[str, List[float]] = {}
    dim = None
    if embed:
        ids_to_embed = [n["id"] for n in nodes if n["id"] in embed_ids]
        texts_to_embed = [_node_text(n) for n in nodes if n["id"] in embed_ids]
        say(f"embedding {len(texts_to_embed)} node texts...")
        vecs = embed.embed_many(texts_to_embed) if texts_to_embed else []
        for nid, vec in zip(ids_to_embed, vecs):
            if vec:
                vec_map[nid] = vec
        dim = embed.dimension
        store.ensure_schema(dim)
    else:
        store.ensure_schema(None)

    node_rows = [{
        "id": n["id"], "kind": n["kind"], "labels": n.get("labels", []),
        "props": n["props"], "embedding": vec_map.get(n["id"]),
    } for n in nodes]
    rel_rows = [{"src": r["src"], "rel": r["rel"], "dst": r["dst"]}
                for r in rels]

    written = store.bulk_merge_nodes(node_rows, batch_size=500)
    say(f"wrote {written}/{len(node_rows)} nodes; linking {len(rel_rows)} relationships...")
    linked = store.bulk_merge_relationships(rel_rows, batch_size=2000)
    say(f"linked {linked} relationships")
    return {"dry_run": False, "counts": counts, "nodes": written,
            "rels": linked, "embedded": len(vec_map), "embedding_dim": dim}


def _node_text(n: Dict[str, Any]) -> str:
    props = n.get("props") or {}
    return (props.get("content") or props.get("value") or props.get("name")
            or props.get("arguments") or "")
