"""Import OpenCode sessions (opencode.db) into the knowledge graph.

OpenCode stores its chats in a SQLite database (``~/.local/share/opencode/
opencode.db``) using three core tables:

    session(id, title, agent, model, directory, project_id, tokens*, cost, time_*)
    message(id, session_id, time_created, data)      -- role/model/token metadata
    part(id, message_id, session_id, time_created, data)  -- the actual content

A ``part.data`` blob is one of:
    text        -> assistant/user prose or tool shell output   (has .text)
    reasoning   -> chain-of-thought                            (has .text)  [GOLD]
    tool        -> a tool call + result (.tool, .callID, .state.input/.output)
    step-start / step-finish / compaction -> control markers   (ignored)

This maps 1:1 onto the knowledge-graph schema built by the provider:

    session  -> sess:oc:<id>            (kind=session, label KgSession)
    message  -> msg:oc:<message_id>     (kind=message, role user/assistant)
    reasoning-> reasoning:oc:<part_id>  (kind=reasoning)      <- embedded (primary)
    tool     -> toolcall:oc:<part_id>   (kind=toolcall)       <- graph-only by default
                toolresult:oc:<part_id> (kind=toolresult)     <- graph-only by default

Edges mirror live capture: FOLLOWED_BY (msg->msg), REASONED (msg->reasoning),
CALLED (msg->toolcall), PRODUCED (toolcall->toolresult), and HAS (session->*).

Embedding policy (per user preference): chain-of-thought and messages are the
recall focus and get embedded; tool input/output is stored in the graph (so it
is reachable by traversal, e.g. kg_related) but is NOT embedded unless
``embed_tools=True`` — a last-resort supplementary surface, not the focus.

The import is idempotent: node ids are derived from stable OpenCode ids, so a
re-run MERGEs the same nodes instead of duplicating them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROVENANCE = "opencode"


def _hash8(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:8]


def _node(node_id: str, kind: str, props: Dict[str, Any],
          labels: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"id": node_id, "kind": kind, "props": props, "labels": labels or []}


def _rel(src: str, rel: str, dst: str) -> Dict[str, Any]:
    return {"src": src, "rel": rel, "dst": dst, "props": {}}


def _parse_model(raw: Any) -> str:
    """session.model is a JSON blob like {"id":"x","providerID":"y"}."""
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
        except Exception:
            return raw
    elif isinstance(raw, dict):
        d = raw
    else:
        return str(raw)
    mid = d.get("id") or ""
    prov = d.get("providerID") or ""
    return f"{prov}/{mid}" if prov and mid else (mid or prov or "")


def _part_text(parts: List[Dict[str, Any]]) -> str:
    out = []
    for p in parts:
        if p.get("type") == "text":
            t = p.get("text")
            if t:
                out.append(t)
    return "\n".join(out).strip()


def _open_ro(db_path: str) -> sqlite3.Connection:
    """Open the OpenCode DB strictly read-only (never mutate the source)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def build_graph(
    db_path: str,
    *,
    since_ms: Optional[int] = None,
    limit_sessions: Optional[int] = None,
    embed_tools: bool = False,
) -> Dict[str, Any]:
    """Read opencode.db and build the node/edge lists + the embed-id set.

    Returns ``{"nodes", "rels", "embed_ids", "sessions", "counts"}``.
    ``embed_ids`` is the subset of node ids whose text should be embedded
    (messages + reasoning, plus tool nodes when ``embed_tools`` is True).
    """
    con = _open_ro(db_path)
    cur = con.cursor()

    where = ""
    params: List[Any] = []
    if since_ms is not None:
        where = "WHERE time_created >= ?"
        params.append(int(since_ms))
    order = "ORDER BY time_created ASC"
    limit = ""
    if limit_sessions:
        limit = "LIMIT ?"
        params_l = list(params) + [int(limit_sessions)]
    else:
        params_l = params
    sessions = cur.execute(
        f"SELECT * FROM session {where} {order} {limit}", params_l
    ).fetchall()

    nodes: List[Dict[str, Any]] = []
    rels: List[Dict[str, Any]] = []
    embed_ids: set = set()
    counts = {"sessions": 0, "messages": 0, "reasoning": 0, "toolcalls": 0,
              "toolresults": 0}

    for srow in sessions:
        sid = srow["id"]
        sess_key = f"oc:{sid}"
        sess_node_id = f"sess:{sess_key}"
        model = _parse_model(srow["model"])
        nodes.append(_node(
            sess_node_id, "session",
            {"session_id": sess_key, "source": _PROVENANCE,
             "oc_session_id": sid,
             "title": srow["title"] or "", "directory": srow["directory"] or "",
             "agent": srow["agent"] or "", "model": model,
             "project_id": srow["project_id"] or "",
             "cost": srow["cost"] or 0,
             "tokens_input": srow["tokens_input"] or 0,
             "tokens_output": srow["tokens_output"] or 0,
             "tokens_reasoning": srow["tokens_reasoning"] or 0,
             "created_at_ms": srow["time_created"] or 0,
             "updated_at_ms": srow["time_updated"] or 0,
             "content": srow["title"] or ""},
            labels=["KgSession"],
        ))
        counts["sessions"] += 1

        messages = cur.execute(
            "SELECT id, data, time_created FROM message WHERE session_id = ? "
            "ORDER BY time_created ASC, id ASC", (sid,),
        ).fetchall()

        prev_msg_node: Optional[str] = None
        for mrow in messages:
            mid = mrow["id"]
            try:
                mdata = json.loads(mrow["data"])
            except Exception:
                mdata = {}
            role = mdata.get("role") or "assistant"
            msg_model = mdata.get("modelID") or model

            parts = cur.execute(
                "SELECT id, data, time_created FROM part WHERE message_id = ? "
                "ORDER BY time_created ASC, id ASC", (mid,),
            ).fetchall()
            parsed_parts = []
            for prow in parts:
                try:
                    parsed_parts.append((prow["id"], json.loads(prow["data"])))
                except Exception:
                    continue

            text_parts = [pd for _, pd in parsed_parts]
            content = _part_text(text_parts)

            msg_node_id = f"msg:oc:{mid}"
            nodes.append(_node(
                msg_node_id, "message",
                {"role": role, "content": content, "session_id": sess_key,
                 "source": _PROVENANCE, "model": msg_model,
                 "oc_message_id": mid, "created_at_ms": mrow["time_created"] or 0},
            ))
            counts["messages"] += 1
            if content:
                embed_ids.add(msg_node_id)
            rels.append(_rel(sess_node_id, "HAS", msg_node_id))
            if prev_msg_node:
                rels.append(_rel(prev_msg_node, "FOLLOWED_BY", msg_node_id))
            prev_msg_node = msg_node_id

            for pid, pd in parsed_parts:
                ptype = pd.get("type")
                if ptype == "reasoning":
                    rtext = (pd.get("text") or "").strip()
                    if not rtext:
                        continue
                    rid = f"reasoning:oc:{pid}"
                    nodes.append(_node(
                        rid, "reasoning",
                        {"content": rtext, "session_id": sess_key,
                         "source": _PROVENANCE, "oc_part_id": pid},
                    ))
                    embed_ids.add(rid)
                    counts["reasoning"] += 1
                    rels.append(_rel(msg_node_id, "REASONED", rid))
                    rels.append(_rel(sess_node_id, "HAS", rid))
                elif ptype == "tool":
                    name = pd.get("tool") or ""
                    state = pd.get("state") or {}
                    tool_input = state.get("input")
                    tid = f"toolcall:oc:{pid}"
                    arg_str = json.dumps(tool_input, ensure_ascii=False)[:4000] \
                        if tool_input is not None else ""
                    nodes.append(_node(
                        tid, "toolcall",
                        {"name": name, "arguments": arg_str,
                         "content": f"{name}({arg_str})"[:4000],
                         "status": state.get("status") or "",
                         "session_id": sess_key, "source": _PROVENANCE,
                         "oc_part_id": pid},
                    ))
                    counts["toolcalls"] += 1
                    if embed_tools:
                        embed_ids.add(tid)
                    rels.append(_rel(msg_node_id, "CALLED", tid))
                    rels.append(_rel(sess_node_id, "HAS", tid))
                    # Tool result (output) -> a distinct graph-only node.
                    output = state.get("output")
                    if output:
                        otext = output if isinstance(output, str) else json.dumps(output)[:8000]
                        rnid = f"toolresult:oc:{pid}"
                        nodes.append(_node(
                            rnid, "toolresult",
                            {"role": "tool_result", "content": otext[:8000],
                             "tool": name, "session_id": sess_key,
                             "source": _PROVENANCE, "oc_part_id": pid},
                        ))
                        counts["toolresults"] += 1
                        if embed_tools:
                            embed_ids.add(rnid)
                        rels.append(_rel(tid, "PRODUCED", rnid))

    con.close()
    return {"nodes": nodes, "rels": rels, "embed_ids": embed_ids,
            "sessions": counts["sessions"], "counts": counts}


def import_opencode(
    store,
    embed,
    db_path: str,
    *,
    since_ms: Optional[int] = None,
    limit_sessions: Optional[int] = None,
    embed_tools: bool = False,
    dry_run: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Import opencode.db into the knowledge graph via ``store``/``embed``.

    ``store`` is a connected ``Neo4jGraphStore``; ``embed`` a
    ``LocalEmbeddingClient`` (or None to skip embeddings entirely).
    """
    def _say(msg: str) -> None:
        if progress:
            progress(msg)

    graph = build_graph(db_path, since_ms=since_ms,
                        limit_sessions=limit_sessions, embed_tools=embed_tools)
    nodes = graph["nodes"]
    rels = graph["rels"]
    embed_ids = graph["embed_ids"]
    counts = graph["counts"]

    _say(f"parsed {counts['sessions']} sessions, {counts['messages']} messages, "
         f"{counts['reasoning']} reasoning, {counts['toolcalls']} tool calls "
         f"({len(embed_ids)} nodes to embed)")

    if dry_run:
        return {"dry_run": True, "counts": counts, "nodes": len(nodes),
                "rels": len(rels), "to_embed": len(embed_ids)}

    # Ensure the vector index exists at the right dimension before writing.
    dim = None
    if embed:
        # Embed everything up-front (batched + cached inside the client).
        vec_map: Dict[str, List[float]] = {}
        ids_to_embed = [n["id"] for n in nodes if n["id"] in embed_ids]
        texts_to_embed = [_node_text(n) for n in nodes if n["id"] in embed_ids]
        _say(f"embedding {len(texts_to_embed)} node texts...")
        vecs = embed.embed_many(texts_to_embed) if texts_to_embed else []
        for nid, vec in zip(ids_to_embed, vecs):
            if vec:
                vec_map[nid] = vec
        dim = embed.dimension
        if dim:
            store.ensure_schema(dim)
    else:
        vec_map = {}
        store.ensure_schema(None)

    # Build bulk write rows.
    node_rows = []
    for n in nodes:
        node_rows.append({
            "id": n["id"],
            "kind": n["kind"],
            "labels": n.get("labels", []),
            "props": n["props"],
            "embedding": vec_map.get(n["id"]),
        })
    rel_rows = [{"src": r["src"], "rel": r["rel"], "dst": r["dst"]} for r in rels]

    # Bulk write (batched UNWIND) — far fewer round-trips than per-row calls.
    written = store.bulk_merge_nodes(node_rows, batch_size=500)
    _say(f"wrote {written}/{len(node_rows)} nodes; linking {len(rel_rows)} relationships...")
    linked = store.bulk_merge_relationships(rel_rows, batch_size=2000)
    _say(f"linked {linked} relationships")

    return {"dry_run": False, "counts": counts, "nodes": written,
            "rels": linked, "embedded": len(vec_map), "embedding_dim": dim}

def _node_text(n: Dict[str, Any]) -> str:
    props = n.get("props") or {}
    return (props.get("content") or props.get("value") or props.get("name")
            or props.get("arguments") or "")
