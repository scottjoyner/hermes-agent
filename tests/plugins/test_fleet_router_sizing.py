from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "fleet-router"


def _load_module():
    name = "test_fleet_router_sizing_impl"
    for module_name in list(sys.modules):
        if module_name == name or module_name.startswith(name + "."):
            sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_context_sizing_counts_tool_schemas():
    module = _load_module()
    messages = [{"role": "user", "content": "inspect the repository"}]
    without_tools = module.core.requirements_from_payload(
        {"model": "coder", "messages": messages}
    )
    with_tools = module.core.requirements_from_payload(
        {
            "model": "coder",
            "messages": messages,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file from the repository. " * 80,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Repository-relative path. " * 40,
                                }
                            },
                            "required": ["path"],
                        },
                    },
                }
            ],
        }
    )

    assert with_tools.input_tokens > without_tools.input_tokens + 200
    assert with_tools.needs_tools is True


def test_context_sizing_does_not_count_base64_image_bytes_as_text_tokens():
    module = _load_module()
    base = {
        "model": "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + ("A" * 500_000)
                        },
                    },
                ],
            }
        ],
    }
    small = module.core.requirements_from_payload(base)
    larger_bytes = {
        **base,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + ("A" * 1_000_000)
                        },
                    },
                ],
            }
        ],
    }
    large = module.core.requirements_from_payload(larger_bytes)

    assert small.needs_vision is True
    assert large.needs_vision is True
    assert small.input_tokens == large.input_tokens
    assert small.input_tokens < 1000


def test_transport_controls_do_not_inflate_context_size():
    module = _load_module()
    minimal = module.core.requirements_from_payload(
        {"model": "coder", "messages": [{"role": "user", "content": "hi"}]}
    )
    controlled = module.core.requirements_from_payload(
        {
            "model": "coder",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.2,
            "top_p": 0.8,
            "seed": 1234,
            "stream": True,
            "max_completion_tokens": 8000,
        }
    )

    assert controlled.input_tokens == minimal.input_tokens
    assert controlled.max_output_tokens == 8000
