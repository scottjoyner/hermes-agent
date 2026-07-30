from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_ROOT = REPO_ROOT / "plugins"
LOADER_PATH = PLUGINS_ROOT / "00-small-model-context" / "__init__.py"
IMPLEMENTATION_PATH = PLUGINS_ROOT / "small-model-context" / "__init__.py"


def _load_ordered_plugin():
    name = "hermes_test_ordered_small_model_context"
    sys.modules.pop(name, None)
    sys.modules.pop("hermes_plugins.small_model_context_impl", None)
    spec = importlib.util.spec_from_file_location(name, LOADER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_optimizer_loader_sorts_before_cache_foundation():
    bundled_directories = sorted(
        path.name for path in PLUGINS_ROOT.iterdir() if path.is_dir()
    )

    assert bundled_directories.index("00-small-model-context") < (
        bundled_directories.index("cache-foundation")
    )
    assert (PLUGINS_ROOT / "00-small-model-context" / "plugin.yaml").is_file()
    assert not (PLUGINS_ROOT / "small-model-context" / "plugin.yaml").exists()
    assert IMPLEMENTATION_PATH.is_file()


def test_ordered_loader_delegates_registration_once():
    plugin = _load_ordered_plugin()
    cli_calls = []
    middleware_calls = []

    class Context:
        def register_cli_command(self, *args, **kwargs):
            cli_calls.append((args, kwargs))

        def register_middleware(self, *args, **kwargs):
            middleware_calls.append((args, kwargs))

    plugin.register(Context())

    assert [call[0][0] for call in cli_calls] == ["context-opt"]
    assert [call[0][0] for call in middleware_calls] == ["llm_request"]
