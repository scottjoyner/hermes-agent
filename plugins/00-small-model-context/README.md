# Small-model context loader

This directory is the discovery entry point for the `small-model-context`
plugin. The implementation and operator guide live in:

```text
plugins/small-model-context/
```

Hermes registers request middleware in bundled plugin discovery order, and
bundled directories are scanned alphabetically. The `00-` prefix is therefore a
correctness constraint: context/schema compaction must run before
`cache-foundation` fingerprints the provider request and attaches cache identity
headers.

The manifest name remains `small-model-context`, so configuration and CLI usage
are unchanged:

```bash
hermes plugins enable small-model-context
hermes context-opt status
```

Do not move or rename this directory without updating and passing
`tests/plugins/test_small_model_context_load_order.py`.
