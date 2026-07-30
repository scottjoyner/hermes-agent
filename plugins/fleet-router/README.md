# Hermes fleet integration

`fleet-router` has three explicit operating modes:

```yaml
fleet:
  mode: external  # external | standalone | disabled
```

## External mode — AssistX/auto-router

Use `external` for the reconciled fleet. Hermes is the executor, not the fleet
router. AssistX/Neo4j owns durable runtime/model identity, access paths,
inventory, capacity, assignments, claims, leases, health, and recovery.
auto-router owns semantic policy routing, physical-runtime admission, LAN-first
and Tailscale-fallback path selection, queueing, and inference telemetry.

```yaml
model:
  provider: custom
  default: auto/code
  base_url: http://auto-router-reconciliation:8088/v1
  context_length: 32768

fleet:
  mode: external
  external:
    base_url: http://auto-router-reconciliation:8088/v1
    admin_url: http://auto-router-reconciliation:8088
    api_key_env: AUTO_ROUTER_CLIENT_TOKEN
    admin_token_env: AUTO_ROUTER_ADMIN_TOKEN
    default_model: auto/code
    strict_offline: true
```

External-mode invariants are enforced:

- `fleet.nodes` is rejected;
- Hermes does not probe physical endpoints;
- Hermes does not track node health, inflight requests, or concurrency slots;
- Hermes does not choose LAN/Tailscale paths or physical models;
- `hermes fleet serve` and `hermes fleet discover` are forbidden;
- `hermes fleet status` reads `/health`, `/v1/models`, and authenticated
  `/admin/admission` from auto-router;
- `hermes fleet route` explains the semantic `auto/*` intent only.

Use aliases such as:

- `auto/fast`
- `auto/high-quality`
- `auto/code`
- `auto/review`
- `auto/iterate`
- `auto/finalize`
- `auto/sophia`
- `auto/local`
- `auto/private`

Task calls should continue to carry task/run, claim/fencing, priority, required
capabilities, `local_only`, workflow-stage, session, and checkpoint metadata.
Hermes cache-identity headers pass through its ordinary provider transport to
auto-router.

Administration:

```bash
hermes plugins enable fleet-router
hermes fleet doctor
hermes fleet status --json
hermes fleet route --model auto/review --tools --reasoning
```

## Standalone mode — Hermes-managed fleet

Use `standalone` only when AssistX and auto-router are not deployed. This keeps
the PR #10 Headroom proxy for independent Hermes installations.

```yaml
model:
  provider: custom
  default: local-coder
  base_url: http://127.0.0.1:8765/v1

fleet:
  mode: standalone
  enabled: true
  health_ttl_seconds: 30
  default_max_output_tokens: 4096
  max_attempts: 2

  listen:
    host: 127.0.0.1
    port: 8765
    allow_non_loopback: false

  nodes:
    - name: primary-large
      base_url: http://primary-large.lan:1234/v1
      provider: lmstudio
      context_length: 32768
      max_concurrency: 1
      prefill_tokens_per_second: 85
      decode_tokens_per_second: 18
      supports_tools: true
      supports_vision: false
      supports_reasoning: true
      models: [local-large-model-id]
      model_map:
        local-coder: local-large-model-id
```

The standalone proxy owns its explicit node registry and supports bounded
concurrent probes, aliases, capability/context eligibility, context-headroom
scoring, throughput and latency hints, inflight pressure, session/checkpoint
affinity, failover, streaming, and cache-header passthrough.

```bash
hermes fleet discover
hermes fleet status --json
hermes fleet route --model local-coder --input-tokens 12000 --max-output-tokens 4000
hermes fleet serve
```

Standalone safety defaults:

- no hosts, tailnets, ports, or machine names are built in;
- only `fleet.nodes` entries are contacted;
- public endpoints require `allow_remote: true` per node;
- the proxy binds to `127.0.0.1` by default;
- non-loopback bind requires `fleet.listen.allow_non_loopback: true`;
- inbound `Authorization` is never forwarded;
- node credentials come only from that node's named `api_key_env`;
- unknown models are rejected unless `accept_unknown_models: true`.

Standalone proxy endpoints:

- `GET /health`
- `GET /fleet/status`
- `GET /v1/models`
- `POST /v1/chat/completions`

See `standalone-fleet-config.yaml.example` for a complete standalone example.

## Disabled mode

```yaml
fleet:
  mode: disabled
```

Hermes uses its ordinary configured provider directly. No fleet plugin routing
or proxy is active.

## Boundary

The standalone router intentionally does not scan subnets, inspect Tailscale
control-plane state, synchronize or load models, mutate runtime health, or move
KV tensors. External mode delegates all fleet state and physical routing to
AssistX/auto-router and never constructs the standalone `FleetRouter` object.
