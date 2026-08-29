# Small-model context optimizer

This bundled, opt-in plugin reduces **always-on** request overhead for smaller
local models without removing capabilities.

It runs through Hermes's public `llm_request` middleware and transforms a copy of
the provider request immediately before dispatch. The transformation is
resolved once per session and deterministic, so the same conversation keeps a
byte-stable request prefix for provider prompt caching.

## What it changes

- Re-renders the always-present skill catalog as names-only or compact entries.
- Shortens tool and nested parameter descriptions to bounded lengths.
- Preserves every tool name, skill name, parameter, type, enum, required field,
  schema branch, message, and conversation turn.
- Never modifies persisted session history or the cached prompt stored on the
  `AIAgent` object.

It does **not** summarize user conversation, remove tool schemas, rewrite memory,
change model routing, or claim to save/restore KV tensors.

## Enable

```bash
hermes plugins enable small-model-context
```

Restart Hermes, then inspect the effective policy:

```bash
hermes context-opt status
hermes context-opt status --json
```

## Configuration

Behavioral settings belong in `~/.hermes/config.yaml`:

```yaml
model:
  provider: lmstudio
  default: local-model-id
  context_length: 32768

agent:
  # auto | lean | balanced | full | off
  context_profile: auto

  # For coding sessions, reduce the active toolset to coding + enabled MCP.
  coding_context: focus

context_optimizer:
  # Optional override of agent.context_profile.
  profile: auto

  # auto | names | compact | full
  skills: auto

  # auto | compact | full
  tools: auto

  # Optional bounds used when the selected mode is compact.
  tool_description_chars: 180
  parameter_description_chars: 96
  skill_description_chars: 120
```

`model.context_length` should be the real total input + output window configured
on the local server. Do not copy a model-family maximum when the running server
was launched with a smaller context.

## Automatic profiles

| Effective context window | Profile | Skill catalog | Tool prose |
| --- | --- | --- | --- |
| `<= 32K` | `lean` | names only | 180-char tool / 96-char parameter descriptions |
| `32K–128K` | `balanced` | descriptions capped at 120 chars | 360-char tool / 160-char parameter descriptions |
| `> 128K` | `full` | unchanged | unchanged |

When context length is unavailable, model IDs containing a parameter count up to
14B select `lean`; models above 70B select `full`; everything else uses
`balanced`. Explicit configuration always wins.

## Project context files

This plugin deliberately does not guess which paragraphs in a project's
`AGENTS.md` are disposable. Use Hermes's existing context-file controls:

1. Put a concise `.hermes.md` or `HERMES.md` at the project root. Hermes loads it
   before `AGENTS.md`.
2. Keep the full human/contributor reference in `AGENTS.md` for on-demand reads.
3. Set an explicit cap when working in repositories you do not control:

```yaml
context_file_max_chars: 6000
```

The root of this fork now follows that pattern: `.hermes.md` is the lean
always-on operating brief; `AGENTS.md` remains the complete upstream reference.

## Measurement

Before and after enabling the plugin, compare:

```text
/context all
```

or:

```bash
hermes prompt-size
```

Focus on the `Skills` and `Tool definitions` categories. The plugin reports the
resolved policy, while Hermes's existing context breakdown remains the source of
truth for whole-request estimates.

## Cache and middleware ordering

The policy is cached by Hermes session ID. Editing `config.yaml` during an active
session therefore does not mutate the request prefix mid-conversation; start a
new session to apply a new profile.

The optimizer returns a normal middleware request replacement. Cache-foundation
fingerprints the final request it receives in middleware order, so any request
whose prompt/schema bytes change naturally receives a different cache identity.

## Safety boundary

Description compaction can remove examples and explanatory nuance. It never
changes schema structure, but unusually complex tools may still perform better
under `balanced` or `full`. Use explicit per-profile configuration rather than
forcing `lean` on every model.
