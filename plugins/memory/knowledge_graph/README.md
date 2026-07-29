# Neo4j Knowledge Graph Memory

This provider gives Hermes a profile-scoped, cross-session knowledge graph without patching the agent loop or gateway. It implements the current public `MemoryProvider` contract and consumes completed turns through `sync_turn(..., messages=...)`.

## Capabilities

- Captures sessions, messages, tool calls/results, delegations, ideas, and indexed documents.
- Writes through a durable SQLite queue under `$HERMES_HOME/knowledge_graph/pending.db` so Neo4j latency or downtime does not block an agent turn.
- Uses Neo4j full-text search by default.
- Adds vector search when one or more OpenAI-compatible local embedding endpoints are configured.
- Fails over through the embedding endpoint list in order.
- Keeps reasoning, raw tool arguments, and raw tool results disabled by default.
- Restricts `kg_query` to read-only Cypher and applies Hermes file-read safety checks during document indexing.

## Installation

The plugin metadata declares the Neo4j Python driver dependency. When installing manually:

```bash
uv pip install "neo4j>=5.20.0,<6"
```

A Neo4j 5.x server is required. The account must be able to create constraints and indexes in the configured database.

## Setup

Run the standard memory-provider setup flow:

```bash
hermes memory setup
```

Select `knowledge_graph`, or configure it directly:

```yaml
memory:
  provider: knowledge_graph

knowledge_graph:
  enabled: true
  uri: bolt://localhost:7687
  user: neo4j
  database: neo4j
  embeddings_base_urls:
    - http://xwing:1234/v1
    - http://tie:1234/v1
  embeddings_model: nomic-embed-text
  capture_reasoning: false
  capture_tool_arguments: false
  capture_tool_results: false
```

Set the password as a secret rather than committing it:

```bash
export NEO4J_PASSWORD='replace-me'
```

Supported environment overrides:

| Variable | Purpose |
|---|---|
| `HERMES_KG_ENABLED` | Enable the provider (`true`, `1`, or `yes`) |
| `NEO4J_URI` | Neo4j Bolt/Neo4j URI |
| `NEO4J_USER` | Neo4j username |
| `NEO4J_PASSWORD` | Neo4j password |
| `NEO4J_DATABASE` | Database name |
| `HERMES_KG_EMBEDDINGS_URLS` | Comma-separated OpenAI-compatible embedding base URLs |
| `HERMES_KG_EMBEDDINGS_MODEL` | Embedding model name |
| `HERMES_KG_EMBEDDINGS_API_KEY` | Optional embedding endpoint key |

`hermes memory setup` writes non-secret provider settings to the active profile's `$HERMES_HOME/knowledge_graph.json`. The durable queue also stays inside that active profile.

## Tools

- `kg_search` — hybrid vector and full-text retrieval.
- `kg_remember` — store an explicit idea, decision, fact, or insight.
- `kg_index_docs` — index Markdown, text, and reStructuredText files.
- `kg_query` — execute bounded read-only Cypher.
- `kg_forget` — delete a node by stable ID.
- `kg_status` — report connection state, privacy settings, and queue depth.

## Privacy defaults

The following settings default to `false`:

- `capture_reasoning` prevents model reasoning fields from being persisted.
- `capture_tool_arguments` replaces raw tool arguments with a redaction marker.
- `capture_tool_results` replaces raw tool output with an omission marker while preserving the graph relationship between the call and its result.

Enabling these settings can persist sensitive model internals, commands, paths, credentials, file contents, or external service responses. Document indexing follows Hermes' read-deny rules, but the graph database itself should still be treated as sensitive application state.

## Failure behavior

Turn capture enqueues locally before Neo4j writes occur. Failed writes stay in SQLite and retry with bounded exponential backoff. Shutdown waits briefly for pending work, then leaves remaining rows durable for the next process start.

Embedding failure does not disable the provider. Search falls back to Neo4j full-text indexes, and write capture continues without vectors.
