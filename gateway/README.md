# gateway/ — Messaging Gateway Runtime

This directory is the messaging-platform integration layer for hermes-agent.
It connects the `AIAgent` runtime to chat platforms (Telegram, Discord,
Slack, WhatsApp, Signal, Matrix, email, webhook, etc.) via per-platform
adapters under `gateway/platforms/`.

## `gateway/run.py` (LLD W-77 — LARGE, ~18.5k LOC)

Central gateway runtime. Owns:

- **Lifecycle**: `start_gateway()` + `GatewayRunner` — startup, shutdown,
  signal handling, runtime-status persistence (`write_runtime_status`).
- **Adapter orchestration**: discover/connect/disconnect every configured
  platform adapter; token-scoped locks per profile.
- **Agent cache**: per-session `AIAgent` construction + caching keyed by
  session signature (see the five `AIAgent(...)` call sites: lines ~8504,
  ~11719, ~12193, ~16611, plus the CLI's own in `cli.py`).
- **Message routing/dispatch**: queue/dispatch, slash-command resolution,
  the two-message-guard bypass, background tasks, cron integration.
- **Status reporting**: agent status callbacks → platform bubbles.

This file was historically undocumented in AGENTS.md. It is far too large to
split in one pass, so extraction is **phased**:

| Submodule | Responsibility | Status |
|-----------|----------------|--------|
| `gateway/status.py` | Status-message helpers (`_prepare_gateway_status_message`, `_send_or_update_status_coro`) | ✅ extracted (W-77) |
| `gateway/lifecycle.py` | Module-level lifecycle helpers (`_gateway_loop_exception_handler`, `_ensure_ssl_certs`, `_reload_runtime_env_preserving_config_authority`, `_start_cron_ticker`) | ✅ extracted (W-77) |
| `gateway/dispatch.py` | message routing + command dispatch | TODO |
| `gateway/session.py` | session store / agent cache | exists (separate file) |

Each extracted submodule re-imports into `run.py` so existing call sites and
behavior are unchanged. New gateway code should land in the appropriate
submodule rather than growing `run.py`.

## Other files
- `gateway/session.py` — session store + transcript persistence.
- `gateway/platforms/` — one adapter per platform (see `ADDING_A_PLATFORM.md`).
- `gateway/builtin_hooks/` — always-registered gateway hooks (none shipped).
- `gateway/config.py` — gateway-specific config bridge (reads user YAML raw;
  candidate for consolidation under `config/loader.py`, see LLD W-84).
