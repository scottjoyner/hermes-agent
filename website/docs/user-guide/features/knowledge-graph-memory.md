---
title: "Neo4j knowledge-graph memory"
description: "Configure the fork's durable Neo4j memory provider with privacy-safe capture and local embedding failover"
---

# Neo4j knowledge-graph memory

The `knowledge_graph` provider adds profile-scoped, cross-session Neo4j memory
without modifying the Hermes agent loop or gateway.

It records sessions, messages, tool-call relationships, delegations, explicit
ideas, and indexed documents. A durable SQLite queue absorbs Neo4j latency or
outages so an agent turn does not block on a graph write.

## Provider selection

Only one external memory provider can be active for a Hermes profile. Built-in
`MEMORY.md` and `USER.md` remain active alongside the selected provider.

```bash
hermes memory setup
```

Select `knowledge_graph`, then confirm:

```bash
hermes memory status
```

Manual selection:

```yaml
memory:
  provider: knowledge_graph
```

## Neo4j requirements

- Neo4j 5.x;
- an account that can create constraints and indexes;
- the Python driver declared by the plugin metadata;
- network access to the selected Bolt or Neo4j URI.

Manual driver installation:

```bash
uv pip install "neo4j>=5.20.0,<6"
```

## Configuration

```yaml
memory:
  provider: knowledge_graph

knowledge_graph:
  enabled: true
  uri: bolt://127.0.0.1:7687
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

Store credentials in `$HERMES_HOME/.env` or a configured secret source:

```bash
NEO4J_PASSWORD=replace-me
HERMES_KG_EMBEDDINGS_API_KEY=
```

Supported environment overrides:

| Variable | Purpose |
| --- | --- |
| `HERMES_KG_ENABLED` | Enable the provider |
| `NEO4J_URI` | Neo4j connection URI |
| `NEO4J_USER` | Neo4j username |
| `NEO4J_PASSWORD` | Neo4j password |
| `NEO4J_DATABASE` | Database name |
| `HERMES_KG_EMBEDDINGS_URLS` | Comma-separated embedding endpoints |
| `HERMES_KG_EMBEDDINGS_MODEL` | Embedding model name |
| `HERMES_KG_EMBEDDINGS_API_KEY` | Optional embedding endpoint key |

The setup flow stores non-secret provider configuration under the active
profile's `$HERMES_HOME/knowledge_graph.json`.

## Retrieval behavior

Full-text indexes are always available. Vector retrieval activates when an
OpenAI-compatible embedding endpoint is configured.

Embedding endpoints are attempted in configured order. If every endpoint
fails, graph writes continue and searches fall back to full text rather than
disabling the provider.

## Tools

| Tool | Purpose |
| --- | --- |
| `kg_search` | Hybrid full-text and vector retrieval |
| `kg_remember` | Store an explicit fact, decision, idea, or insight |
| `kg_index_docs` | Index Markdown, text, and reStructuredText files |
| `kg_query` | Run bounded read-only Cypher |
| `kg_forget` | Delete one graph node by stable ID |
| `kg_status` | Show connection, privacy, and queue posture |

Primary sessions receive all tools. Subagent, cron, and memory-flush contexts
are read-only and receive only `kg_search`, `kg_query`, and `kg_status`.

## Privacy defaults

These settings default to false and should ordinarily remain false:

```yaml
knowledge_graph:
  capture_reasoning: false
  capture_tool_arguments: false
  capture_tool_results: false
```

Enabling them can persist model internals, commands, paths, credentials, file
contents, or third-party service responses. The graph database must be treated
as sensitive application state even when these fields are disabled.

`kg_query` rejects mutating Cypher. Document indexing applies Hermes file-read
denial rules before loading content.

## Durable queue and backup

Pending graph writes are stored at:

```text
$HERMES_HOME/knowledge_graph/pending.db
```

A complete backup needs both:

1. a Neo4j database backup or dump;
2. the active profile's `knowledge_graph/pending.db`.

The queue can contain writes that have been accepted locally but not yet
committed to Neo4j. Backing up only the graph can therefore lose the newest
pending memories.

Failed writes retry with bounded exponential backoff. Shutdown waits briefly
for pending work, then leaves unfinished rows durable for the next process.

## Operational verification

```bash
hermes fork-doctor
hermes memory status
```

Then ask Hermes to run `kg_status` from a primary session. Verify:

- Neo4j is connected;
- queue depth does not continuously increase;
- privacy flags match policy;
- embedding endpoints are healthy when vector search is expected;
- read-only contexts do not expose mutation tools.

## Relationship to cache and Headroom

Neo4j recall belongs in the volatile request zone. It should not be baked into
the reusable stable prompt prefix, because recalled memories vary by turn and
would invalidate otherwise reusable cache state.

A future Headroom integration should optimize the live conversation tail while
preserving Neo4j's durable source records and the cache foundation's stable
prefix and tool-schema identities.
