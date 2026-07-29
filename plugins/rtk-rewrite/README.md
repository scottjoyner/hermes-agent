# RTK command-output compression for Hermes

This bundled plugin integrates [RTK (Rust Token Killer)](https://github.com/rtk-ai/rtk) directly with Hermes terminal workflows.

RTK recognizes more than 100 common CLI command families and compresses noisy output before it enters the model context. Examples include Git, GitHub CLI, pytest, Ruff, Cargo, npm/pnpm, Docker, Kubernetes, AWS, Terraform/Pulumi, log readers, grep, file listings, and build systems.

The Rust binary remains the source of truth for command detection and filtering. Hermes calls `rtk rewrite` in a `pre_tool_call` hook and mutates the terminal command only when RTK returns a supported rewrite. This avoids duplicating RTK's command catalog in Python and means newly supported RTK commands become available after updating the binary.

## Enable the bundled plugin

```bash
hermes plugins enable rtk-rewrite
```

Restart Hermes after enabling it. Bundled Hermes plugins are opt-in, so simply updating the repository does not change terminal behavior until this command is run.

## Install RTK

After enabling the plugin:

```bash
hermes rtk install
```

The installer prefers Homebrew when available, then Cargo. You can select one explicitly:

```bash
hermes rtk install --method brew
hermes rtk install --method cargo
```

The Cargo path installs directly from `https://github.com/rtk-ai/rtk`, avoiding the unrelated crate with the same name on crates.io.

You can also install RTK using its upstream installation instructions, then restart Hermes.

## Commands

```bash
hermes rtk status
hermes rtk doctor
hermes rtk rewrite git status
hermes rtk gain
hermes rtk gain --graph
```

- `status` reports the resolved binary, version, rewrite state, and timeout.
- `doctor` verifies that `rtk rewrite` can rewrite a representative command.
- `rewrite` previews RTK's decision without executing the command.
- `gain` shows RTK's estimated command-output savings analytics.

Once enabled and healthy, normal Hermes terminal calls are rewritten automatically. The agent can continue issuing ordinary commands such as `git status`, `pytest`, `ruff check .`, or `docker ps`; it does not need to prefix them with `rtk`.

## Fail-open guarantees

The original terminal command runs unchanged when:

- RTK is not installed or disappears from `PATH`.
- `rtk rewrite` times out.
- RTK returns its documented passthrough result.
- RTK returns malformed or empty output.
- The tool is not Hermes's `terminal` tool.
- The payload has no non-empty string `command`.
- Any unexpected adapter exception occurs.

The adapter invokes RTK with an argument vector and `shell=False`; it never evaluates the command itself.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `HERMES_RTK_DISABLE` | unset | Disable rewriting while leaving the plugin and CLI commands installed |
| `HERMES_RTK_BINARY` | `rtk` | Alternate binary name or path resolved through `PATH` |
| `HERMES_RTK_REWRITE_TIMEOUT` | `2` | Rewrite-decision timeout in seconds, clamped to 0.1–10 |

## Relationship to upstream RTK

RTK already provides a user-local Hermes installer through `rtk init --agent hermes`. This fork bundles an equivalent integration in the Hermes repository and adds native `hermes rtk` administration commands. Do not run the upstream installer on top of this bundled plugin unless you intentionally want a user plugin under `~/.hermes/plugins/rtk-rewrite/` to override the bundled copy.

RTK is licensed under Apache-2.0. This integration does not vendor the Rust binary or its filtering implementation.
