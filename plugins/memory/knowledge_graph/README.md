# Knowledge Graph Session Capture + Backfill

This plugin captures Hermes sessions into Neo4j as a searchable graph:

- `(:KgSession {id: "sess:<session_id>"})`
- `(:KgNode {kind: "message"})` for user/assistant messages
- `(:KgNode {kind: "reasoning"})` for reasoning / CoT fields when capture is enabled
- `(:KgNode {kind: "toolcall"})` and `(:KgNode {kind: "toolresult"})` for tool provenance
- `(:KgSession)-[:HAS]->(...)`, `(:Message)-[:FOLLOWED_BY]->(:Message)`,
  `(:Message)-[:REASONED]->(:Reasoning)`, `(:Message)-[:CALLED]->(:ToolCall)-[:PRODUCED]->(:ToolResult)`

## Live session switching

`KnowledgeGraphMemoryProvider.on_session_switch()` keeps the provider's cached
session id in sync when Hermes changes `AIAgent.session_id` without recreating
the provider, including `/resume`, `/branch`, `/new`, `/reset`, and compression
continuations.

Switches enqueue an idempotent session upsert with:

- `session_id`
- `parent_session_id`
- `platform`
- `profile`
- `model`
- `last_event`
- `reset`

Lineage is represented as:

- `(:KgSession)-[:DERIVED_TO]->(:KgSession)` for resume/branch/compression-style continuity
- `(:KgSession)-[:RESET_TO]->(:KgSession)` for explicit reset/new-session boundaries

## Shutdown behavior

`on_session_end()` only marks the session finalized and persists sequence state.
It deliberately does not replay the entire message list, because live capture ids
are sequence-derived and a full-tail replay can duplicate message nodes. Use the
SQLite backfill below for historical repair/reconciliation.

## SQLite `state.db` backfill

`session_backfill.py` imports Hermes' primary SQLite session store into Neo4j.
It opens the DB read-only and derives stable node ids from SQLite primary keys,
so it is safe to re-run.

Default embedding policy:

- embedded: user/assistant messages and reasoning fields
- graph-only: tool calls and tool results
- optional: set `embed_tools=True` to embed tool call/result content too

Programmatic usage:

```python
from plugins.memory.knowledge_graph.session_backfill import import_state_db

result = import_state_db(
    store,
    embed,
    "/path/to/state.db",
    dry_run=True,          # review counts first
    limit_sessions=100,    # optional incremental run
)
```

Agent tool usage once the KG provider is active:

```json
{"db_path": "/path/to/state.db", "dry_run": true}
```

via `kg_import_sessions`. The tool defaults to `$HERMES_HOME/state.db` and
`dry_run=true`; explicitly pass `dry_run=false` to write to Neo4j.

## Verification

Targeted tests:

```bash
uv run --extra dev python -m pytest \
  tests/plugins/memory/test_knowledge_graph.py \
  tests/plugins/memory/test_session_backfill.py \
  tests/plugins/memory/test_opencode_import.py -q
```

Syntax check:

```bash
uv run --extra dev python -m compileall -q \
  plugins/memory/knowledge_graph \
  tests/plugins/memory/test_session_backfill.py
```
