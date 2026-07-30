# Hermes fleet router

`fleet-router` exposes a stable local OpenAI-compatible endpoint and routes each
request to the best explicitly configured inference node. It replaces the old
in-process fleet implementation, which depended on personal host defaults and
outdated provider internals.

The proxy keeps Hermes provider transports unchanged. Hermes talks to one local
URL, while the proxy handles node health, model mapping, context headroom,
concurrency pressure, latency, throughput hints, streaming, failover, and
cache/session affinity.

## Safety defaults

- No hosts, tailnets, ports, or machine names are built in.
- Only nodes listed under `fleet.nodes` are contacted.
- Public endpoints are rejected unless that individual node sets
  `allow_remote: true`.
- The proxy binds to `127.0.0.1` by default.
- A non-loopback bind requires `fleet.listen.allow_non_loopback: true`.
- Inbound `Authorization` is never forwarded upstream.
- A node receives a bearer token only when it names a dedicated
  `api_key_env` variable.
- `OPENAI_API_KEY` and unrelated provider keys are never forwarded implicitly.
- Cache identity headers may pass through so an inference server or cache-aware
  controller can reuse exact prefixes.

## Enable and inspect

```bash
hermes plugins enable fleet-router
hermes fleet doctor
hermes fleet discover
hermes fleet status --json
```

Explain routing without sending a request:

```bash
hermes fleet route \
  --model local-coder \
  --input-tokens 12000 \
  --max-output-tokens 4000 \
  --tools \
  --reasoning
```

Run the proxy:

```bash
hermes fleet serve
```

The default endpoint is:

```text
http://127.0.0.1:8765/v1
```

Configure Hermes to use that endpoint while keeping the requested model as a
stable fleet alias:

```yaml
model:
  provider: custom
  default: local-coder
  base_url: http://127.0.0.1:8765/v1
  context_length: 32768
```

Use a conservative `model.context_length` for the main Hermes process. The
router separately rejects nodes that cannot fit the estimated input, requested
output, and a safety margin.

## Configuration

```yaml
fleet:
  enabled: true
  health_ttl_seconds: 30
  default_max_output_tokens: 4096
  max_attempts: 2

  listen:
    host: 127.0.0.1
    port: 8765
    allow_non_loopback: false
    # token_env: HERMES_FLEET_LISTEN_TOKEN

  nodes:
    - name: x1-370
      base_url: http://x1-370.lan:1234/v1
      provider: lmstudio
      context_length: 32768
      max_concurrency: 1
      prefill_tokens_per_second: 85
      decode_tokens_per_second: 18
      supports_tools: true
      supports_vision: false
      supports_reasoning: true
      models:
        - qwen3.5-35b-a3b
      model_map:
        local-coder: qwen3.5-35b-a3b

    - name: macbook-air
      base_url: http://macbook-air.lan:1234/v1
      provider: lmstudio
      context_length: 16384
      max_concurrency: 2
      prefill_tokens_per_second: 120
      decode_tokens_per_second: 28
      models:
        - qwen3.5-2b
      model_map:
        local-fast: qwen3.5-2b

    - name: protected-node
      base_url: http://protected-node.lan:8000/v1
      api_key_env: PROTECTED_NODE_API_KEY
      context_length: 65536
      max_concurrency: 1
      models:
        - private-model
```

Static `models` allow deterministic routing before or when `/v1/models` is
unavailable. Successful probes replace the runtime catalog with the server's
reported model IDs. `model_map` defines stable client-facing aliases without
rewriting the Hermes conversation or model configuration.

Set `accept_unknown_models: true` only for a node that intentionally accepts
arbitrary model IDs. The default is fail-closed.

## Route scoring

A node must first pass hard eligibility checks:

- healthy and recently probed;
- requested model or explicit alias available;
- tools, vision, and reasoning capabilities satisfied;
- configured context length large enough for estimated input, requested output,
  and safety margin.

Eligible nodes are then ranked by:

- remaining context headroom;
- exact model match;
- operator priority;
- configured prefill and decode throughput;
- measured latency exponential moving average;
- current in-flight requests relative to concurrency;
- session affinity;
- cache-checkpoint affinity.

The score is deterministic, and ties resolve by node name. `hermes fleet route`
prints the ranked candidates and reasons.

## Proxy endpoints

- `GET /health`
- `GET /fleet/status`
- `GET /v1/models`
- `POST /v1/chat/completions`

Both normal JSON and streaming chat-completion responses are relayed. The
response includes `X-Hermes-Fleet-Node` so operators can see which node served
the request.

The current implementation intentionally does not scan an entire subnet,
inspect Tailscale state, persist credentials, synchronize model files, or claim
to move KV tensors between engines. Those require separate operator-controlled
services and exact engine/model/quantization compatibility.
