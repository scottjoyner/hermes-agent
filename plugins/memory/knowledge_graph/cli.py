"""CLI for the knowledge-graph memory provider.

Exposes ``hermes knowledge-graph`` with verbs:
    status     — connection + graph stats
    search     — semantic + graph search (needs local embeddings)
    related    — graph neighbors of a node
    query      — raw Cypher (read analytics)
    ideas      — list recorded ideas + their links
    forget     — delete a node by id
    visualize  — export a Cytoscape JSON of the graph (for inspection)

Registration is automatic: when ``memory.provider: knowledge_graph`` is set,
``hermes_cli.main`` calls ``register_cli`` here and routes
``hermes knowledge-graph ...`` to ``knowledge_graph_command``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

_PROVIDER_NAME = "knowledge_graph"


def _load_bits():
    """Lazily import the plugin's store/embedder + config (avoids native deps at CLI import time)."""
    from hermes_cli.config import cfg_get, load_config
    from plugins.memory.knowledge_graph.embeddings import LocalEmbeddingClient
    from plugins.memory.knowledge_graph.graph_store import Neo4jGraphStore

    config = load_config()
    kg = cfg_get(config, "knowledge_graph") or {}

    def _neo4j_params() -> Dict[str, str]:
        import os

        return {
            "uri": kg.get("uri") or os.environ.get("NEO4J_URI") or "bolt://localhost:7687",
            "user": kg.get("user") or os.environ.get("NEO4J_USER") or "neo4j",
            "password": kg.get("password") or os.environ.get("NEO4J_PASSWORD") or "",
            "database": kg.get("database") or os.environ.get("NEO4J_DATABASE") or "neo4j",
        }

    def _embed() -> Optional[LocalEmbeddingClient]:
        emb = kg.get("embeddings") or {}
        return LocalEmbeddingClient(
            base_url=emb.get("base_url") or "",
            model=emb.get("model") or "",
            api_key=emb.get("api_key") or "",
            timeout=float(emb.get("timeout") or 30.0),
            batch_size=int(emb.get("batch_size") or 32),
            registry_path=emb.get("registry_path") or "",
        )

    return Neo4jGraphStore, _neo4j_params, _embed


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes knowledge-graph`` argparse tree."""
    sub = subparser.add_subparsers(dest="kg_command")

    sub.add_parser("status", help="Show connection + graph statistics").set_defaults(
        kg_action="status"
    )

    p_search = sub.add_parser("search", help="Semantic + graph search")
    p_search.add_argument("query", help="Free-text query")
    p_search.add_argument("--top-k", type=int, default=8)
    p_search.add_argument("--scope", choices=["both", "docs", "cot"], default="both",
                         help="Blend docs + chain-of-thought (both), or restrict to one")
    p_search.add_argument("--doc-weight", type=float, default=0.6,
                         help="Doc blend weight when --scope both (default: 0.6)")
    p_search.add_argument("--cot-weight", type=float, default=0.4,
                         help="CoT blend weight when --scope both (default: 0.4)")
    p_search.add_argument("--kinds", nargs="*", default=None,
                         help="Power-user override: restrict to raw node kinds "
                              "(message reasoning toolcall idea entity session doc docchunk)")
    p_search.add_argument("--expand", action="store_true", help="Also show graph neighbors")
    p_search.set_defaults(kg_action="search")

    p_rel = sub.add_parser("related", help="Show graph neighbors of a node")
    p_rel.add_argument("--node-id", default="", help="Node id from search")
    p_rel.add_argument("--text", default="", help="Locate nearest node by text first")
    p_rel.add_argument("--rel-types", nargs="*", default=None)
    p_rel.add_argument("--limit", type=int, default=30)
    p_rel.set_defaults(kg_action="related")

    p_q = sub.add_parser("query", help="Run a raw Cypher query")
    p_q.add_argument("cypher", help="Cypher query (use read-only in production)")
    p_q.add_argument("--limit", type=int, default=50)
    p_q.set_defaults(kg_action="query")

    p_ideas = sub.add_parser("ideas", help="List recorded ideas + their links")
    p_ideas.add_argument("--limit", type=int, default=50)
    p_ideas.add_argument("--tag", default="", help="Filter by tag")
    p_ideas.set_defaults(kg_action="ideas")

    p_forget = sub.add_parser("forget", help="Delete a node by id")
    p_forget.add_argument("node_id", help="Node id to delete")
    p_forget.set_defaults(kg_action="forget")

    p_idx = sub.add_parser("index-docs", help="Index .md/.txt docs into the graph")
    p_idx.add_argument("paths", nargs="+", help="Files or directories")
    p_idx.add_argument("--no-recursive", dest="recursive", action="store_false",
                      help="Do not recurse into directories")
    p_idx.add_argument("--glob", default="*.md", help="File glob (default: *.md)")
    p_idx.set_defaults(kg_action="index_docs")

    p_rd = sub.add_parser("read-doc", help="Resolve a doc to its real file path")
    p_rd.add_argument("--path", default="", help="Local file path")
    p_rd.add_argument("--doc-id", default="", help="Doc node id from search")
    p_rd.add_argument("--snippet-chars", type=int, default=600)
    p_rd.set_defaults(kg_action="read_doc")

    p_viz = sub.add_parser("visualize", help="Export Cytoscape JSON of the graph")
    p_viz.add_argument("--limit", type=int, default=500, help="Max nodes")
    p_viz.add_argument("--out", default="kg_graph.json", help="Output JSON path")
    p_viz.set_defaults(kg_action="visualize")

    p_oc = sub.add_parser(
        "import-opencode",
        help="Import OpenCode sessions (opencode.db) into the graph",
    )
    p_oc.add_argument(
        "--db", default="",
        help="Path to opencode.db (default: ~/.local/share/opencode/opencode.db)",
    )
    p_oc.add_argument("--since", default="",
                     help="Only sessions created on/after this ISO date (YYYY-MM-DD)")
    p_oc.add_argument("--limit", type=int, default=0,
                     help="Max number of sessions to import (0 = all)")
    p_oc.add_argument("--embed-tools", action="store_true",
                     help="Also embed tool input/output (default: graph-only, "
                          "recall focuses on chain-of-thought + messages)")
    p_oc.add_argument("--dry-run", action="store_true",
                     help="Parse and report counts without writing to Neo4j")
    p_oc.set_defaults(kg_action="import_opencode")

    p_hs = sub.add_parser(
        "import-sessions",
        help="Reconcile Hermes state.db sessions into the graph",
    )
    p_hs.add_argument("--db", default="",
                      help="Path to state.db (default: active Hermes home)")
    p_hs.add_argument("--since-ts", type=float, default=None,
                      help="Only sessions whose started_at is at/after this Unix timestamp")
    p_hs.add_argument("--limit", type=int, default=0,
                      help="Max sessions to reconcile (0 = all)")
    p_hs.add_argument("--embed-tools", action="store_true",
                      help="Also embed tool calls/results")
    p_hs.add_argument("--no-embed", action="store_true",
                      help="Write graph structure without embeddings")
    p_hs.add_argument("--write", action="store_true",
                      help="Write to Neo4j (default is a dry run)")
    p_hs.set_defaults(kg_action="import_sessions")

    p_exp = sub.add_parser(
        "export-finetune",
        help="Export OpenCode sessions (opencode.db) to a finetuning JSONL corpus",
    )
    p_exp.add_argument("--db", default="",
                       help="Path to opencode.db (default: ~/.local/share/opencode/opencode.db)")
    p_exp.add_argument("--out", default="finetune_corpus.jsonl",
                      help="Output JSONL path")
    p_exp.add_argument("--format", choices=["openai", "sharegpt"], default="openai",
                       help="Training-example format (default: openai)")
    p_exp.add_argument("--no-reasoning", action="store_true",
                      help="Do not surface chain-of-thought as reasoning_content")
    p_exp.add_argument("--include-tool-io", action="store_true",
                      help="Include tool call + result turns in the conversation flow")
    p_exp.add_argument("--min-messages", type=int, default=2,
                      help="Drop sessions with fewer than this many turns")
    p_exp.add_argument("--max-examples", type=int, default=0,
                      help="Stop after this many examples (0 = all)")
    p_exp.add_argument("--since", default="",
                      help="Only sessions created on/after this ISO date (YYYY-MM-DD)")
    p_exp.add_argument("--limit", type=int, default=0,
                      help="Max number of sessions to export (0 = all)")
    p_exp.add_argument("--dry-run", action="store_true",
                      help="Report counts without writing the file")
    p_exp.set_defaults(kg_action="export_finetune")


def knowledge_graph_command(args: argparse.Namespace) -> None:
    """Dispatch ``hermes knowledge-graph <verb>``."""
    action = getattr(args, "kg_action", None) or "status"
    Neo4jGraphStore, neo4j_params, make_embed = _load_bits()

    if action == "status":
        store = Neo4jGraphStore(**neo4j_params())
        try:
            store.connect()
            stats = store.stats()
        finally:
            store.close()
        print("\nKnowledge Graph status\n" + "─" * 44)
        print(f"  Connected: {stats.get('connected')}")
        if stats.get("connected"):
            print(f"  Nodes:     {stats.get('nodes')}")
            print(f"  Edges:     {stats.get('relationships')}")
            print(f"  Emb. dim:  {stats.get('embedding_dim')}")
            by_kind = stats.get("by_kind") or {}
            if by_kind:
                print("  By kind:")
                for k, v in sorted(by_kind.items(), key=lambda x: -x[1]):
                    print(f"    {k:<12} {v}")
        else:
            print("  Neo4j not reachable. Check NEO4J_URI / knowledge_graph.uri.")
        print()
        return

    # The remaining verbs need a live connection.
    store = Neo4jGraphStore(**neo4j_params())
    if not store.connect():
        print("  Neo4j not reachable. Check NEO4J_URI / knowledge_graph.uri.\n")
        return

    try:
        if action == "search":
            embed = make_embed()
            vec = embed.embed(args.query) if embed else None
            if not vec:
                print("  Embeddings unavailable (LM Studio / Ollama not reachable).")
                print("  Set LMSTUDIO_EMBEDDINGS_BASE_URL + _MODEL, or run a local embeddings model.\n")
                return
            _DOC_KINDS = {"docchunk", "doc"}
            _CTX_KINDS = {"reasoning", "message", "idea"}
            if args.kinds:
                # Power-user override: raw kinds, unweighted.
                hits = store.vector_search(vec, top_k=args.top_k, kinds=args.kinds)
            else:
                scope = args.scope
                kinds = []
                if scope in ("both", "docs"):
                    kinds += list(_DOC_KINDS)
                if scope in ("both", "cot"):
                    kinds += list(_CTX_KINDS)
                pool = max(args.top_k * 2, 24)
                raw = store.vector_search(vec, top_k=pool, kinds=kinds)
                if scope == "docs":
                    dw, cw = 1.0, 0.0
                elif scope == "cot":
                    dw, cw = 0.0, 1.0
                else:
                    tot = (args.doc_weight + args.cot_weight) or 1.0
                    dw, cw = args.doc_weight / tot, args.cot_weight / tot
                for h in raw:
                    w = dw if h.get("kind") in _DOC_KINDS else (
                        cw if h.get("kind") in _CTX_KINDS else 0.5)
                    h["weighted_score"] = round(float(h.get("score", 0.0)) * w, 4)
                raw.sort(key=lambda x: x["weighted_score"], reverse=True)
                hits = raw[:args.top_k]
            if not hits:
                print("  No matches.\n")
                return
            print(f"\nSearch: {args.query!r}  (scope={args.scope})\n" + "─" * 44)
            for h in hits:
                score = h.get("weighted_score", h.get("score", 0.0))
                print(f"  ({h['kind']} {score:.3f}) {str(h['content'])[:120]}")
                if h.get("path"):
                    print(f"      path={h['path']}")
                print(f"      id={h['id']}")
                if args.expand:
                    for n in store.neighbors(h["id"], limit=8):
                        print(f"      → {n['kind']}: {str(n['content'])[:80]}")
            print()
            return

        if action == "related":
            node_id = args.node_id
            if not node_id and args.text:
                embed = make_embed()
                vec = embed.embed(args.text) if embed else None
                if vec:
                    h = store.vector_search(vec, top_k=1)
                    if h:
                        node_id = h[0]["id"]
            if not node_id:
                print("  Provide --node-id or --text.\n")
                return
            nbrs = store.neighbors(node_id, rel_types=args.rel_types or None, limit=args.limit)
            print(f"\nNeighbors of {node_id}\n" + "─" * 44)
            for n in nbrs:
                print(f"  {n['kind']:<10} {str(n['content'])[:100]}")
            print()
            return

        if action == "query":
            rows = store.run_cypher(args.cypher[:8000])
            print(f"\n{len(rows)} row(s)\n" + "─" * 44)
            for r in rows[:args.limit]:
                print("  " + json.dumps({k: _truncate(v) for k, v in r.items()}, ensure_ascii=False))
            print()
            return

        if action == "ideas":
            q = (
                "MATCH (i:Idea) "
                "OPTIONAL MATCH (i)-[:LINKS_TO]->(e:Entity) "
                "RETURN i.id AS id, i.content AS content, i.tags AS tags, "
                "collect(DISTINCT {kind: e.kind, value: e.value}) AS links "
                "ORDER BY i.created_at DESC LIMIT $limit"
            )
            rows = store.run_cypher(q, {"limit": args.limit})
            print(f"\nIdeas ({len(rows)})\n" + "─" * 44)
            for r in rows:
                if args.tag and args.tag not in (r.get("tags") or []):
                    continue
                print(f"  • {str(r.get('content'))[:100]}")
                print(f"    id={r.get('id')}")
                for link in (r.get("links") or []):
                    if link.get("kind"):
                        print(f"    → [{link['kind']}] {link.get('value')}")
            print()
            return

        if action == "forget":
            store.run_cypher("MATCH (n {id: $id}) DETACH DELETE n", {"id": args.node_id})
            print(f"  Deleted {args.node_id}\n")
            return

        if action == "index_docs":
            embed = make_embed()
            if not embed:
                print("  Embeddings unavailable (LM Studio / Ollama not reachable).\n")
                return
            # Reuse the provider's ingestion by calling its tool dispatcher.
            from plugins.memory.knowledge_graph import KnowledgeGraphMemoryProvider

            p = KnowledgeGraphMemoryProvider()
            p._cfg = {
                "embeddings": {},
                "capture": {"sessions": True, "chain_of_thought": True,
                            "tool_calls": True, "user_messages": True,
                            "assistant_messages": True},
            }
            p._available = True
            p._session_id = "cli"
            p._embed = embed
            p._store = store  # share the live connection
            p._enqueue_job = lambda sid, job: p._apply_job(job)  # type: ignore[method-assign]
            # Apply synchronously against the live store.
            orig_apply = p._apply_job
            for path in args.paths:
                out = p._dispatch("kg_index_docs", {
                    "paths": [path], "recursive": args.recursive, "glob": args.glob,
                })
                # kg_index_docs only enqueues; flush its job right away.
                # Pull the last enqueued job is awkward — instead apply directly:
                # re-run with the worker pointed at the store.
                print(f"  {path}: {out}")
            return

        if action == "import_sessions":
            import os as _os
            from hermes_constants import get_hermes_home
            from plugins.memory.knowledge_graph.session_backfill import import_state_db

            db = args.db or str(get_hermes_home() / "state.db")
            if not _os.path.exists(db):
                print(f"  state.db not found at {db}\n")
                return
            dry_run = not bool(args.write)
            embed = None if args.no_embed or dry_run else make_embed()
            progress: List[str] = []
            out = import_state_db(
                store,
                embed,
                db,
                since_ts=args.since_ts,
                limit_sessions=args.limit or None,
                embed_tools=bool(args.embed_tools),
                dry_run=dry_run,
                progress=progress.append,
            )
            for line in progress:
                print(f"  {line}")
            print(json.dumps(out, indent=2, default=str))
            return

        if action == "import_opencode":
            import os as _os
            from datetime import datetime as _dt
            from plugins.memory.knowledge_graph.opencode_import import import_opencode

            db = args.db or _os.path.expanduser("~/.local/share/opencode/opencode.db")
            if not _os.path.exists(db):
                print(f"  opencode.db not found at {db}\n")
                return
            since_ms = None
            if args.since:
                try:
                    since_ms = int(_dt.fromisoformat(args.since).timestamp() * 1000)
                except Exception:
                    print(f"  Invalid --since date: {args.since!r} (use YYYY-MM-DD)\n")
                    return
            embed = None if args.dry_run else make_embed()
            if not args.dry_run and embed is not None:
                # Warm up so we fail fast if LM Studio is unreachable.
                if embed.embed("ping") is None:
                    print("  Embeddings endpoint unreachable — writing graph-only "
                          "(no vectors).")
                    embed = None
            print(f"\nImporting OpenCode sessions from {db}")
            print("─" * 44)
            out = import_opencode(
                store, embed, db,
                since_ms=since_ms,
                limit_sessions=(args.limit or None),
                embed_tools=args.embed_tools,
                dry_run=args.dry_run,
                progress=lambda m: print(f"  {m}"),
            )
            print("─" * 44)
            if out.get("dry_run"):
                print(f"  DRY RUN — would import: {out['counts']}")
                print(f"  nodes={out['nodes']} rels={out['rels']} "
                      f"to_embed={out['to_embed']}")
            else:
                print(f"  imported: {out['counts']}")
                print(f"  nodes={out['nodes']} rels={out['rels']} "
                      f"embedded={out.get('embedded')} dim={out.get('embedding_dim')}")
            print()
            return

        if action == "export_finetune":
            import os as _os
            from datetime import datetime as _dt
            from plugins.memory.knowledge_graph.finetune_export import (
                export_finetune, build_export_config)

            db = args.db or _os.path.expanduser("~/.local/share/opencode/opencode.db")
            if not _os.path.exists(db):
                print(f"  opencode.db not found at {db}\n")
                return
            since_ms = None
            if args.since:
                try:
                    since_ms = int(_dt.fromisoformat(args.since).timestamp() * 1000)
                except Exception:
                    print(f"  Invalid --since date: {args.since!r} (use YYYY-MM-DD)\n")
                    return
            cfg = build_export_config(
                format=args.format,
                include_reasoning=not args.no_reasoning,
                include_tool_io=args.include_tool_io,
                min_messages=args.min_messages,
                max_examples=(args.max_examples or None),
            )
            print(f"\nExporting finetuning corpus from {db}")
            print("─" * 44)
            out = export_finetune(
                db, args.out, config=cfg, since_ms=since_ms,
                limit_sessions=(args.limit or None), dry_run=args.dry_run,
            )
            print("─" * 44)
            print(f"  sessions seen: {out.get('sessions_seen')}  "
                  f"examples: {out.get('examples')}  skipped: {out.get('skipped')}")
            if out.get("dry_run"):
                print("  DRY RUN — nothing written.")
            else:
                print(f"  wrote: {out.get('out')}  (format={out.get('format')})")
            print()
            return

        if action == "read_doc":
            out = store.run_cypher(
                "MATCH (d:doc) WHERE d.path = $p OR d.id = $id "
                "RETURN d.path AS p LIMIT 1",
                {"p": args.path, "id": args.doc_id},
            )
            path = args.path
            if not path and out:
                path = out[0].get("p") or ""
            if not path:
                print("  Provide --path or --doc-id.\n")
                return
            from pathlib import Path as _P

            p = _P(path).expanduser()
            if not p.exists():
                print(f"  File not found: {p}\n")
                return
            text = p.read_text(encoding="utf-8", errors="replace")
            print(f"\nDoc: {p} ({len(text)} chars)\n" + "─" * 44)
            print("  OPEN THE FULL SOURCE with read_file before answering.")
            print(f"  Snippet:\n{text[:args.snippet_chars]}\n")
            return

        if action == "visualize":
            nodes = store.run_cypher(
                "MATCH (n:KgNode) RETURN n.id AS id, n.kind AS kind, "
                "n.content AS content LIMIT $limit", {"limit": args.limit}
            )
            edges = store.run_cypher(
                "MATCH (a)-[r]->(b) RETURN a.id AS s, b.id AS t, "
                "type(r) AS rel LIMIT $limit", {"limit": args.limit}
            )
            cy: Dict[str, Any] = {"nodes": [], "edges": []}
            for n in nodes:
                cy["nodes"].append({
                    "data": {
                        "id": n.get("id"), "label": n.get("kind"),
                        "content": _truncate(n.get("content"), 160),
                    }
                })
            for e in edges:
                cy["edges"].append({
                    "data": {
                        "id": f"{e.get('s')}->{e.get('t')}",
                        "source": e.get("s"), "target": e.get("t"),
                        "label": e.get("rel"),
                    }
                })
            out = args.out
            with open(out, "w", encoding="utf-8") as f:
                json.dump(cy, f, ensure_ascii=False, indent=2)
            print(f"  Wrote {len(cy['nodes'])} nodes / {len(cy['edges'])} edges to {out}\n")
            return
    finally:
        store.close()


def _truncate(v: Any, n: int = 80) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + "…"
    return v
