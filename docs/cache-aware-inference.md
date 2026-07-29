# Cache-aware inference developer contract

This document defines the compatibility boundary between Hermes Agent and any
inference server, checkpoint controller, or fleet proxy that consumes the
`cache-foundation` manifest.

The contract is deliberately independent of a particular inference engine.
Hermes identifies reusable request structure and records evidence; a backend
adapter owns model-specific KV memory and persistence.

## Design goals

1. Keep inference-engine details out of the planner and conversation loop.
2. Preserve byte-stable Hermes prompt prefixes across turns and sessions.
3. Prevent incompatible weights, templates, engines, or KV layouts from sharing
   a checkpoint identifier.
4. Keep session affinity stronger than small latency differences.
5. Record only hashes and operational metadata in Hermes state.
6. Fail open for inference execution and fail closed for cache compatibility.
7. Never execute the downstream provider twice because cache telemetry failed.

## Middleware boundary

The plugin uses two public middleware kinds:

- `llm_request` computes the manifest and attaches cache headers.
- `llm_execution` wraps the provider call once and records its result.

The request middleware is allowed to alter only `extra_headers`. The execution
middleware must call `next_call(request)` no more than once.

The implementation currently applies only to `chat_completions`. Native
Anthropic, Bedrock Converse, Codex Responses, and other wire modes remain
untouched until an adapter defines equivalent semantics.

## Header namespace

Hermes owns every header beginning with:

```text
X-Hermes-Cache-
```

A middleware or caller may supply unrelated provider headers, but a preexisting
Hermes cache header is overwritten by the current manifest. This prevents stale
or forged checkpoint/session identifiers from becoming authoritative.

Current headers:

| Header | Meaning |
| --- | --- |
| `X-Hermes-Cache-Schema` | Manifest schema version |
| `X-Hermes-Cache-Checkpoint-Id` | Compatible reusable static checkpoint identity |
| `X-Hermes-Cache-Prefix-Id` | Exact replay-prefix identity before the newest item |
| `X-Hermes-Cache-System-Hash` | Full outgoing system/developer message hash |
| `X-Hermes-Cache-Static-Hash` | Reusable stable system-prefix hash |
| `X-Hermes-Cache-Tool-Hash` | Ordered tool-schema hash |
| `X-Hermes-Cache-Engine` | Engine family or configured controller ID |
| `X-Hermes-Cache-Mode` | Operator policy hint, currently defaulting to `prefer` |
| `X-Hermes-Cache-Session-Id` | Opaque Hermes session identifier |

Headers must never contain prompt text, tool payloads, model responses, or API
keys.

## Canonical hashing

JSON-shaped values use:

- UTF-8;
- `ensure_ascii=False`;
- sorted mapping keys;
- compact separators;
- original list order.

Sorting mapping keys removes irrelevant dictionary insertion-order differences.
Preserving list order is mandatory because message order, content-part order,
and tool order can change tokenization and model behavior.

## Checkpoint identity

The checkpoint ID hashes:

```text
schema version
model fingerprint
engine fingerprint
chat-template hash
KV format/layout
stable system-prefix hash
tool-schema hash
API mode
```

It intentionally excludes:

- Hermes session ID;
- endpoint address;
- volatile system suffix;
- current user message;
- tool results and later conversation messages.

The same compatible checkpoint may therefore be reusable across multiple
sessions and nodes.

A backend must reject checkpoint reuse when any identity component differs.
A low-level loader must still validate its own engine-native metadata before
mapping memory or loading tensors.

## Request-prefix identity

The request-prefix ID hashes:

```text
checkpoint ID
all replayed messages before the newest conversational item
ordered tool schema
```

This ID is useful for managed in-memory caches and routing. It is not a claim
that the full request was identical, because the newest item is deliberately
outside the reusable prefix.

## Stable system-prefix sources

Manifest construction chooses the stable prefix in this order:

1. an explicit runtime-provided `stable_system_prefix`;
2. a provider-decorated static content block;
3. the full system/developer message.

The bundled plugin can supply an operator-owned exact prefix from
`HERMES_CACHE_STABLE_PREFIX_FILE`. It uses the file only when the outgoing
system message starts with the file contents byte-for-byte. Otherwise it falls
back to the full system message.

The intended future core bridge is to pass
`agent._cached_system_prompt_static` through the LLM middleware context. That
bridge must preserve the same `build_manifest(..., stable_system_prefix=...)`
contract and must not put the raw prefix into telemetry.

## Endpoint trust boundary

Cache identifiers can correlate sessions. By default, the plugin permits only:

- loopback;
- private and link-local IP addresses;
- Tailscale `100.64.0.0/10` addresses;
- `.lan`, `.local`, `.internal`, and `.ts.net` names;
- single-label LAN hostnames.

Provider branding is never a trust signal. `provider="lmstudio"` does not make a
public URL eligible.

Remote cache proxies require `HERMES_CACHE_ALLOW_REMOTE=1`. Future adapters
should reuse `is_cache_eligible_endpoint()` instead of implementing a looser
policy.

## Durable state schema

The SQLite state database contains three logical tables:

### `session_affinity`

Maps one Hermes session to its latest endpoint, provider, model, checkpoint ID,
and request-prefix ID.

### `checkpoints`

Tracks checkpoint ID plus endpoint, model, engine, evidence state, and maximum
observed prefix tokens.

### `requests`

Tracks one API request ID, duration, success flag, token counts, cache token
counts, and a bounded error string.

Prompt text and provider responses are never columns in this database.

## Inventory evidence states

| State | Required evidence |
| --- | --- |
| `observed` | Hermes produced a manifest for the endpoint |
| `warmed` | Explicit warmup request completed |
| `written` | Provider reported cache creation/write tokens |
| `hit` | Provider reported cache-read tokens |
| `restored` | Backend adapter positively confirmed checkpoint restoration |

Adapters must not promote `observed` or `warmed` to `written` based solely on
latency or the existence of a candidate file path.

## Pending request handoff

Request middleware and execution middleware are separate phases. The plugin
keeps an in-memory map from `api_request_id` to manifest so execution telemetry
uses the exact request manifest.

The map is capped at 1,024 entries. If cancellation or another middleware stops
a request before execution, oldest entries are evicted rather than growing a
long-running gateway without bound.

A future high-concurrency implementation may replace this map with a bounded
TTL cache, but it must preserve:

- opaque request IDs;
- no raw request bodies;
- bounded memory;
- exactly-once provider execution.

## Usage normalization

The execution wrapper recognizes common provider fields:

- OpenAI `prompt_tokens` and `completion_tokens`;
- Responses/Anthropic-style `input_tokens` and `output_tokens`;
- `prompt_tokens_details.cached_tokens`;
- `input_tokens_details.cached_tokens`;
- `cache_read_input_tokens`;
- `cache_creation_input_tokens`;
- `cache_write_tokens`.

Missing detailed cache fields must be recorded as zero. An adapter should not
invent a cache hit from elapsed time.

## Route scoring

`CacheRouteCandidate` returns a lower-is-better cost. The current scoring order
is intentionally dominated by locality:

1. healthy candidates only;
2. session affinity;
3. checkpoint presence;
4. model residency;
5. queue depth;
6. estimated prefill cost;
7. cold-load cost;
8. network latency.

The numerical weights are policy defaults, not a wire protocol. A fleet
provider may calibrate them, but changing the priority order should require
benchmarks demonstrating that migration cost is genuinely lower than keeping
session locality.

## llama.cpp adapter requirements

A pinned llama.cpp controller should expose operations equivalent to:

```text
inspect runtime fingerprint
inspect loaded model fingerprint
save compatible checkpoint
restore compatible checkpoint
list checkpoint inventory
delete checkpoint
report cache/prefill metrics
```

Before restore, validate at least:

- model file hash and quantization;
- tokenizer/chat-template identity;
- llama.cpp build or serialization version;
- context size;
- KV key/value types;
- architecture-specific metadata;
- checkpoint integrity and expected byte size.

Restore failure must fall back to ordinary prefill. It must not prevent Hermes
from completing the provider request.

## LM Studio and Ollama adapters

LM Studio and Ollama may manage prompt/KV reuse internally without exposing a
portable checkpoint API. Their first adapters should therefore be observational:

- discover loaded models and runtime metadata;
- keep session affinity;
- send deterministic manifest headers to a trusted sidecar/proxy;
- collect runtime-native metrics where available;
- avoid claiming disk persistence without an explicit API.

A sidecar may later translate Hermes headers into engine-specific controls.

## Headroom integration boundary

Headroom should be implemented as separate request middleware. Its allowed zone
is the volatile/live conversation tail.

It must not rewrite:

- the selected stable prefix;
- tool definitions included in the checkpoint ID;
- already compressed RTK terminal output merely for cache alignment;
- durable Neo4j records.

Recommended middleware ordering:

1. Headroom transforms the live tail deterministically.
2. Cache foundation fingerprints the resulting provider request.
3. Observer hooks record the effective request.
4. Cache/fleet execution middleware routes and executes once.

If Headroom runs after cache foundation, it must force manifest recomputation.
The safer default is to register it before cache foundation.

## Test requirements for adapters

Every adapter should cover:

- incompatible model/template/KV fingerprints never reuse a checkpoint;
- trusted and untrusted endpoint classification;
- session affinity survives process restart when intended;
- cache metadata never stores prompt text or credentials;
- restore failure falls back to normal prefill;
- telemetry failure never duplicates provider execution;
- cancellation does not leak unbounded state;
- cache hits are supported by backend evidence;
- cold, warm, restored, and invalidated benchmark cases.

## Benchmark matrix

Measure at least:

| Mode | Purpose |
| --- | --- |
| Baseline | No cache-foundation plugin or adapter |
| Manifest only | Plugin enabled; backend ignores headers |
| Runtime-managed cache | LM Studio/Ollama/native prefix reuse |
| Explicit warm | Prefix evaluated before the task |
| Explicit restore | llama.cpp or sidecar confirms checkpoint restore |
| Headroom only | Live-zone optimization without checkpoint routing |
| Combined | RTK + Headroom + cache-aware routing |

Record time to first token, prefill tokens per second, total wall time, peak RAM
and VRAM, cache-read/write tokens, output quality, and failure/fallback rate.
