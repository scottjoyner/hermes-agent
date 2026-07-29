---
sidebar_position: 13
title: Cache-aware inference
description: Reuse local inference prefixes safely with deterministic manifests, session affinity, warmup, and cache telemetry.
---

# Cache-aware inference

Hermes can attach deterministic cache manifests to local OpenAI-compatible
inference requests. The manifests let a local runtime or fleet proxy recognize
reusable prompt prefixes, preserve session affinity, and report real cache
reads and writes without putting model-specific KV tensor logic inside the
agent loop.

The feature is provided by the bundled, opt-in `cache-foundation` plugin.

## What this feature is

Cache-aware inference gives Hermes a common vocabulary for:

- the model weights and quantization currently loaded;
- the inference-engine build and KV representation;
- the chat template used to serialize messages;
- the reusable stable system-prompt prefix;
- the active tool schema;
- the replayed conversation prefix;
- the endpoint currently holding a session;
- provider-reported cache reads and writes.

These values become deterministic hashes and identifiers. A compatible
inference controller can use them to decide whether a warm prefix or restored
checkpoint can be reused.

## What this feature is not

The plugin does not serialize model attention tensors and does not assume that
llama.cpp, LM Studio, Ollama, vLLM, or another runtime share a universal KV
checkpoint format.

It also does not automatically migrate a live session between machines. The
included route-scoring contract is a foundation for a later fleet provider or
proxy.

A checkpoint state of `observed` or `warmed` is not proof that a file was
written to disk. Only backend evidence should advance inventory to `written`,
`restored`, or `hit`.

## Enable the plugin

```bash
hermes plugins enable cache-foundation
hermes cache status
```

Bundled plugins are disabled by default. Enabling the plugin registers:

- LLM request middleware that creates the cache manifest;
- LLM execution middleware that records duration and usage telemetry;
- the `hermes cache` command group.

Disable it without deleting state:

```bash
hermes plugins disable cache-foundation
```

Or keep the CLI available while bypassing middleware:

```bash
export HERMES_CACHE_DISABLE=1
```

## Endpoint trust policy

Hermes sends cache identifiers only to endpoints classified as local or private
by default:

- loopback addresses such as `127.0.0.1` and `::1`;
- RFC1918 private networks;
- link-local addresses;
- Tailscale's `100.64.0.0/10` range;
- names ending in `.lan`, `.local`, `.internal`, or `.ts.net`;
- single-label LAN hostnames such as `x1-370`.

The configured provider name is not a trust signal. A public URL remains
ineligible even when its provider is labeled `lmstudio` or `ollama`.

A trusted remote cache proxy requires explicit opt-in:

```bash
export HERMES_CACHE_ALLOW_REMOTE=1
```

This opt-in discloses opaque session and prefix identifiers to that endpoint.
It does not send prompt text in the headers.

## Inspect configuration

```bash
hermes cache doctor \
  --endpoint http://x1-370:8080 \
  --provider custom
```

The command reports:

- whether middleware is enabled;
- whether the endpoint passes the trust policy;
- the inferred engine family;
- whether control is `managed` or `explicit`;
- stable-prefix file status;
- reachability of common model and runtime endpoints.

`managed` means Hermes can observe cache behavior but the runtime owns its
internal cache lifecycle. `explicit` is reserved for an adapter or controller
that can deliberately save and restore compatible checkpoints.

## Define the deployment fingerprint

For in-memory provider caches, the configured model name may be sufficient for
initial experimentation. Reusable disk checkpoints need a stricter identity:

```bash
export HERMES_CACHE_ENGINE_ID=llama.cpp
export HERMES_CACHE_ENGINE_FINGERPRINT=llama.cpp-b6123
export HERMES_CACHE_MODEL_FINGERPRINT='Qwen3.5-35B-A3B-Q4_K_M:sha256:...'
export HERMES_CACHE_CHAT_TEMPLATE_HASH='sha256:...'
export HERMES_CACHE_KV_FORMAT='q8_0-k/q4_0-v'
```

Changing any value creates a different checkpoint ID. This prevents Hermes from
claiming compatibility across different weights, tokenizers, templates, engine
builds, or KV layouts.

## Reuse the stable Hermes system prefix

Hermes builds its system prompt in three tiers:

1. **Stable** — identity, tool guidance, skills, platform guidance, and other
   cross-session instructions.
2. **Context** — project files, workspace context, and caller-provided system
   instructions.
3. **Volatile** — memory snapshots, user profile information, session metadata,
   and timestamps.

Some provider-native cache formats expose the stable tier as a separately
marked content block. Plain local OpenAI-compatible runtimes normally receive a
single system string. For those runtimes, configure an exact prefix file:

```bash
mkdir -p ~/.hermes/cache/prompts
export HERMES_CACHE_STABLE_PREFIX_FILE=~/.hermes/cache/prompts/primary-cli-coding.txt
```

The file is accepted only when its contents exactly match the beginning of the
outgoing system message. A mismatch falls back to the full-system hash. Hermes
never silently labels a different prefix as reusable.

The file is read as UTF-8, capped at 4 MiB, and cached by path, size, and
modification time. Status output contains only its path, length, and SHA-256.
The text is not copied into the cache-state database.

## Warm a prefix

```bash
hermes cache warm \
  --endpoint http://x1-370:8080 \
  --model qwen3.5-35b-a3b \
  --prompt-file ~/.hermes/cache/prompts/primary-cli-coding.txt
```

Warmup sends a one-token Chat Completions request with temperature zero. It is
useful for:

- evaluating a large stable prefix before an interactive session begins;
- populating a runtime-managed in-memory prompt cache;
- validating endpoint and model configuration;
- measuring the first uncached prefill cost.

Warmup records `warmed`, not `written`. A runtime adapter must confirm actual
checkpoint persistence before claiming a disk checkpoint exists.

For a local endpoint requiring authentication:

```bash
export HERMES_CACHE_API_KEY='local-runtime-key'
```

This key is used only by the explicit `cache warm` command. Hermes applies its
credential-safe redirect policy, so authorization is not forwarded across
origins.

## Read status and telemetry

```bash
hermes cache status --json
```

Important fields include:

| Field | Meaning |
| --- | --- |
| `affinities` | Sessions currently associated with an endpoint |
| `checkpoints` | Distinct endpoint/checkpoint inventory rows |
| `requests` | Provider calls recorded by execution middleware |
| `prompt_tokens` | Provider-reported prompt/input tokens |
| `cache_read_tokens` | Tokens the provider says were served from cache |
| `cache_write_tokens` | Tokens the provider says were added to cache |
| `average_duration_ms` | Mean provider-call duration in the local database |
| `pending_requests` | Manifests waiting to reach execution middleware |
| `stable_prefix.loaded` | Whether the configured prefix file was read |

Inspect recent calls:

```bash
hermes cache inspect --session SESSION_ID --limit 20
```

Inspect checkpoint inventory:

```bash
hermes cache checkpoints --limit 20
```

## Inventory states

| State | Safe interpretation |
| --- | --- |
| `observed` | Hermes generated a compatible manifest for this endpoint |
| `warmed` | A deliberate warmup evaluated the prefix |
| `written` | Provider telemetry reported cache creation or write tokens |
| `hit` | Provider telemetry reported cache-read tokens |
| `restored` | A backend adapter confirmed a checkpoint restore |

Do not infer `written` or `restored` from low latency alone. Latency can change
because of queue depth, thermal limits, model residency, or network conditions.

## Session affinity and future fleet routing

The shared route score prefers:

1. a node already holding the session;
2. a node with the compatible checkpoint;
3. a node with the model already loaded;
4. lower queue, prefill, and cold-load cost;
5. lower network latency.

This ordering avoids moving a session merely because another node is a few
milliseconds closer. The cost of rebuilding a long prefix generally dominates
small transport differences.

The plugin records affinity but does not change the active provider URL. A
future fleet proxy or provider will consume the same manifest and route score.

## Interaction with RTK, Headroom, and Neo4j

The components intentionally own different parts of context management:

- **RTK** compresses terminal output before it becomes conversation history.
- **Headroom** will optimize the volatile/live message zone in a separate
  integration.
- **Neo4j** stores durable semantic and relational memory.
- **Cache foundation** identifies and measures reusable inference prefixes.

Headroom should not rewrite the stable prefix or tool schema after a checkpoint
ID is selected. RTK output should not be compressed a second time solely to
increase cache hits. Neo4j recall should remain in the volatile user-message
zone so durable memory does not invalidate the stable prefix.

## Clear local state

Clear affinity and request telemetry for one session:

```bash
hermes cache clear --session SESSION_ID
```

Clear all affinity and request telemetry:

```bash
hermes cache clear
```

Also remove checkpoint inventory:

```bash
hermes cache clear --checkpoints
```

These commands remove Hermes metadata only. They do not delete runtime-owned KV
files or unload models.

## Troubleshooting

### No cache headers appear

Check that:

- `cache-foundation` is enabled;
- `HERMES_CACHE_DISABLE` is not set;
- the request uses `chat_completions` mode;
- the endpoint passes `hermes cache doctor`;
- another middleware did not replace the complete request after this plugin.

### Stable prefix reports `loaded: false`

Verify that the file:

- exists and is readable as UTF-8;
- is non-empty and no larger than 4 MiB;
- is configured in the same environment that launches Hermes.

A loaded file can still be rejected for a specific request when it does not
exactly prefix the outgoing system message. In that case the manifest source is
`full-system` rather than `runtime-static`.

### Cache reads remain zero

Zero means the provider did not report cache-read tokens. It does not always
mean the provider failed to reuse internal state; some local APIs omit detailed
usage fields. Use runtime-native metrics before drawing a conclusion.

### `warmed` never becomes `written`

This is expected for managed runtimes that do not report cache-creation tokens.
A llama.cpp checkpoint adapter or cache-aware proxy must provide explicit
persistence evidence to advance the state.

### Pending requests are nonzero

A small transient count is normal while requests move from request middleware
to execution middleware. The map is capped at 1,024 entries, and oldest entries
are evicted if requests are abandoned before execution.
