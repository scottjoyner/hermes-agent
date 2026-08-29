"""Cache-safe request compaction for smaller local models.

The optimizer runs in Hermes's public ``llm_request`` middleware seam. It does
not remove tools, parameters, skills, messages, or conversation history. It
only shortens descriptive text that is resent on every request:

* the always-on skill catalog is rendered as names-only or compact entries;
* tool and parameter descriptions are bounded while names, types, enums,
  required fields, and schemas remain intact.

A policy is resolved once per Hermes session so the transformed request prefix
stays byte-stable for prompt caching. Behavioral settings live in config.yaml,
not environment variables.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Mapping, MutableMapping, Sequence

_PROFILE_NAMES = {"auto", "lean", "balanced", "full", "off"}
_SKILL_MODES = {"auto", "names", "compact", "full"}
_TOOL_MODES = {"auto", "compact", "full"}
_POLICY_CACHE_MAX = 1024
_POLICY_CACHE: "OrderedDict[str, ContextPolicy]" = OrderedDict()
_POLICY_LOCK = threading.RLock()

_SKILLS_SECTION_RE = re.compile(
    r"## Skills \(mandatory\).*?</available_skills>",
    re.DOTALL,
)
_AVAILABLE_SKILLS_RE = re.compile(
    r"<available_skills>\s*(.*?)\s*</available_skills>",
    re.DOTALL,
)
_MODEL_SIZE_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[bB](?![A-Za-z])")


@dataclass(frozen=True)
class ContextPolicy:
    """Deterministic compaction policy for one agent session."""

    profile: str
    context_length: int
    skill_mode: str
    tool_mode: str
    tool_description_chars: int
    parameter_description_chars: int
    skill_description_chars: int
    source: str = "auto"

    @property
    def enabled(self) -> bool:
        return self.profile not in {"full", "off"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _load_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        value = load_config_readonly() or {}
    except Exception:
        try:
            from hermes_cli.config import load_config

            value = load_config() or {}
        except Exception:
            value = {}
    return value if isinstance(value, dict) else {}


def _model_size_billions(model: str) -> float | None:
    """Best-effort parameter-count hint from common model IDs."""

    matches = _MODEL_SIZE_RE.findall(model or "")
    if not matches:
        return None
    try:
        # Model IDs can mention both total and active MoE parameters. The larger
        # number better represents reasoning capability for this conservative
        # fallback; explicit config/context length always wins.
        return max(float(value) for value in matches)
    except ValueError:
        return None


def _auto_profile(context_length: int, model: str) -> str:
    """Choose a conservative profile from window size, then model size."""

    if context_length:
        if context_length <= 32_768:
            return "lean"
        if context_length <= 131_072:
            return "balanced"
        return "full"

    size = _model_size_billions(model)
    if size is not None and size <= 14:
        return "lean"
    if size is not None and size > 70:
        return "full"
    return "balanced"


def _profile_defaults(profile: str) -> tuple[str, str, int, int, int]:
    if profile == "lean":
        return "names", "compact", 180, 96, 0
    if profile == "balanced":
        return "compact", "compact", 360, 160, 120
    return "full", "full", 0, 0, 0


def resolve_policy(
    *,
    config: Mapping[str, Any] | None = None,
    model: str = "",
    context_length: int = 0,
) -> ContextPolicy:
    """Resolve the effective policy without mutating configuration."""

    cfg = dict(config) if isinstance(config, Mapping) else _load_config()
    model_cfg = _mapping(cfg.get("model"))
    optimizer_cfg = _mapping(cfg.get("context_optimizer"))
    agent_cfg = _mapping(cfg.get("agent"))

    configured_profile = str(
        optimizer_cfg.get("profile")
        or agent_cfg.get("context_profile")
        or "auto"
    ).strip().lower()
    if configured_profile not in _PROFILE_NAMES:
        configured_profile = "auto"

    effective_context = (
        _integer(context_length)
        or _integer(optimizer_cfg.get("context_length"))
        or _integer(model_cfg.get("context_length"))
    )
    effective_model = str(model or model_cfg.get("default") or "")

    if configured_profile == "auto":
        profile = _auto_profile(effective_context, effective_model)
        source = "auto"
    else:
        profile = configured_profile
        source = "config"

    default_skills, default_tools, tool_chars, param_chars, skill_chars = (
        _profile_defaults(profile)
    )

    skill_mode = str(optimizer_cfg.get("skills") or "auto").strip().lower()
    if skill_mode not in _SKILL_MODES:
        skill_mode = "auto"
    if skill_mode == "auto":
        skill_mode = default_skills

    tool_mode = str(optimizer_cfg.get("tools") or "auto").strip().lower()
    if tool_mode not in _TOOL_MODES:
        tool_mode = "auto"
    if tool_mode == "auto":
        tool_mode = default_tools

    if tool_mode == "compact":
        tool_chars = _integer(
            optimizer_cfg.get("tool_description_chars"),
            tool_chars,
        )
        param_chars = _integer(
            optimizer_cfg.get("parameter_description_chars"),
            param_chars,
        )
    else:
        tool_chars = 0
        param_chars = 0

    if skill_mode == "compact":
        skill_chars = _integer(
            optimizer_cfg.get("skill_description_chars"),
            skill_chars,
        )
    else:
        skill_chars = 0

    return ContextPolicy(
        profile=profile,
        context_length=effective_context,
        skill_mode=skill_mode,
        tool_mode=tool_mode,
        tool_description_chars=tool_chars,
        parameter_description_chars=param_chars,
        skill_description_chars=skill_chars,
        source=source,
    )


def _policy_key(kwargs: Mapping[str, Any]) -> str:
    session_id = str(kwargs.get("session_id") or "").strip()
    if session_id:
        return f"session:{session_id}"
    return "fallback:" + "|".join(
        [
            str(kwargs.get("provider") or ""),
            str(kwargs.get("model") or ""),
            str(kwargs.get("base_url") or ""),
        ]
    )


def _policy_for_request(kwargs: Mapping[str, Any]) -> ContextPolicy:
    key = _policy_key(kwargs)
    with _POLICY_LOCK:
        cached = _POLICY_CACHE.get(key)
        if cached is not None:
            _POLICY_CACHE.move_to_end(key)
            return cached

    policy = resolve_policy(model=str(kwargs.get("model") or ""))
    with _POLICY_LOCK:
        _POLICY_CACHE[key] = policy
        _POLICY_CACHE.move_to_end(key)
        while len(_POLICY_CACHE) > _POLICY_CACHE_MAX:
            _POLICY_CACHE.popitem(last=False)
    return policy


def _reset_policy_cache_for_tests() -> None:
    with _POLICY_LOCK:
        _POLICY_CACHE.clear()


def _collapse_space(text: str) -> str:
    return " ".join(str(text or "").split())


def _shorten(text: str, limit: int) -> str:
    clean = _collapse_space(text)
    if not limit or len(clean) <= limit:
        return clean
    window = clean[: limit + 1]
    sentence_end = max(window.rfind(". "), window.rfind("; "))
    if sentence_end >= int(limit * 0.55):
        return window[: sentence_end + 1].rstrip()
    word_end = window.rfind(" ")
    if word_end >= int(limit * 0.7):
        window = window[:word_end]
    return window.rstrip(" ,;:-") + "…"


def _parse_skills(body: str) -> dict[str, list[tuple[str, str]]]:
    categories: dict[str, list[tuple[str, str]]] = {}
    category = "general"
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("-"):
            payload = line[1:].strip()
            name, sep, description = payload.partition(":")
            name = name.strip()
            if name:
                categories.setdefault(category, []).append(
                    (name, description.strip() if sep else "")
                )
            continue

        label, sep, tail = line.partition(":")
        if not sep:
            continue
        category = label.replace("[names only]", "").strip() or "general"
        # Existing names-only lines put comma-separated names after the colon.
        if "[names only]" in label:
            for name in (part.strip() for part in tail.split(",")):
                if name:
                    categories.setdefault(category, []).append((name, ""))
        else:
            categories.setdefault(category, [])
    return categories


def _render_skills(
    categories: Mapping[str, Sequence[tuple[str, str]]],
    policy: ContextPolicy,
) -> str:
    lines: list[str] = []
    for category in sorted(categories):
        dedup: dict[str, str] = {}
        for name, description in categories[category]:
            dedup.setdefault(name, description)
        if policy.skill_mode == "names":
            lines.append(f"  {category}: {', '.join(sorted(dedup))}")
            continue
        lines.append(f"  {category}:")
        for name in sorted(dedup):
            description = _shorten(
                dedup[name],
                policy.skill_description_chars,
            )
            suffix = f": {description}" if description else ""
            lines.append(f"    - {name}{suffix}")

    if policy.skill_mode == "names":
        intro = (
            "## Skills\n"
            "Load a matching skill with `skill_view(name)` before acting. "
            "Names remain available even when descriptions are omitted.\n\n"
        )
    else:
        intro = (
            "## Skills\n"
            "Before acting, load any matching skill with `skill_view(name)`.\n\n"
        )
    return (
        intro
        + "<available_skills>\n"
        + "\n".join(lines)
        + "\n</available_skills>"
    )


def compact_skills_prompt(text: str, policy: ContextPolicy) -> str:
    if policy.skill_mode == "full" or not text:
        return text
    section = _SKILLS_SECTION_RE.search(text)
    if section is None:
        return text
    available = _AVAILABLE_SKILLS_RE.search(section.group(0))
    if available is None:
        return text
    categories = _parse_skills(available.group(1))
    if not categories:
        return text
    replacement = _render_skills(categories, policy)
    return text[: section.start()] + replacement + text[section.end() :]


def _compact_descriptions(
    value: Any,
    *,
    top_limit: int,
    nested_limit: int,
    depth: int = 0,
) -> Any:
    if isinstance(value, list):
        return [
            _compact_descriptions(
                item,
                top_limit=top_limit,
                nested_limit=nested_limit,
                depth=depth + 1,
            )
            for item in value
        ]
    if not isinstance(value, Mapping):
        return value

    result: dict[str, Any] = {}
    for key, item in value.items():
        if key == "description" and isinstance(item, str):
            limit = top_limit if depth <= 2 else nested_limit
            result[key] = _shorten(item, limit)
        else:
            result[key] = _compact_descriptions(
                item,
                top_limit=top_limit,
                nested_limit=nested_limit,
                depth=depth + 1,
            )
    return result


def compact_tools(tools: Any, policy: ContextPolicy) -> Any:
    if policy.tool_mode != "compact" or not isinstance(tools, list):
        return tools
    return _compact_descriptions(
        tools,
        top_limit=policy.tool_description_chars,
        nested_limit=policy.parameter_description_chars,
    )


def _compact_message_content(content: Any, policy: ContextPolicy) -> tuple[Any, bool]:
    if isinstance(content, str):
        updated = compact_skills_prompt(content, policy)
        return updated, updated != content
    if not isinstance(content, list):
        return content, False

    changed = False
    updated_parts: list[Any] = []
    for part in content:
        if isinstance(part, str):
            updated = compact_skills_prompt(part, policy)
            changed = changed or updated != part
            updated_parts.append(updated)
        elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
            updated_part = dict(part)
            updated_text = compact_skills_prompt(str(part["text"]), policy)
            updated_part["text"] = updated_text
            changed = changed or updated_text != part["text"]
            updated_parts.append(updated_part)
        else:
            updated_parts.append(part)
    return updated_parts, changed


def optimize_request(
    request: Mapping[str, Any],
    policy: ContextPolicy,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Return a transformed copy and deterministic size telemetry."""

    updated = copy.deepcopy(dict(request))
    before = len(json.dumps(request, ensure_ascii=False, default=str))
    changed = False

    for key in ("messages", "input"):
        messages = updated.get(key)
        if not isinstance(messages, list):
            continue
        for index, message in enumerate(messages):
            if not isinstance(message, MutableMapping):
                continue
            role = str(message.get("role") or "").lower()
            if role not in {"system", "developer"}:
                continue
            content, content_changed = _compact_message_content(
                message.get("content"),
                policy,
            )
            if content_changed:
                copied = dict(message)
                copied["content"] = content
                messages[index] = copied
                changed = True
            break

    if "tools" in updated:
        optimized_tools = compact_tools(updated.get("tools"), policy)
        if optimized_tools != updated.get("tools"):
            updated["tools"] = optimized_tools
            changed = True

    after = len(json.dumps(updated, ensure_ascii=False, default=str))
    return updated, {
        "changed": int(changed),
        "before_chars": before,
        "after_chars": after,
        "saved_chars": max(0, before - after),
    }


def _optimize_request(**kwargs: Any) -> dict[str, Any] | None:
    request = kwargs.get("request")
    if not isinstance(request, Mapping):
        return None
    policy = _policy_for_request(kwargs)
    if not policy.enabled:
        return None

    updated, telemetry = optimize_request(request, policy)
    if not telemetry["changed"]:
        return None
    return {
        "request": updated,
        "source": "small-model-context",
        "reason": (
            f"applied {policy.profile} context profile; "
            f"saved approximately {telemetry['saved_chars']} serialized characters"
        ),
    }


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="context_opt_command")
    status = commands.add_parser(
        "status",
        help="Show the effective small-model context policy",
    )
    status.add_argument("--json", action="store_true")
    parser.set_defaults(func=_handle_cli)


def _handle_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "context_opt_command", None)
    if command != "status":
        print("usage: hermes context-opt status [--json]")
        return 2

    config = _load_config()
    model_cfg = _mapping(config.get("model"))
    policy = resolve_policy(
        config=config,
        model=str(model_cfg.get("default") or ""),
        context_length=_integer(model_cfg.get("context_length")),
    )
    payload = {
        **policy.to_dict(),
        "plugin_enabled": True,
        "session_policy_cache_entries": len(_POLICY_CACHE),
        "session_policy_cache_limit": _POLICY_CACHE_MAX,
    }
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("Hermes small-model context optimizer")
        print("------------------------------------")
        for key, value in payload.items():
            print(f"{key:34}: {value}")
    return 0


def register(ctx: Any) -> None:
    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        register_cli(
            "context-opt",
            "Inspect small-model prompt and schema compaction",
            _setup_cli,
            _handle_cli,
            description="Cache-safe context optimization for smaller models",
        )
    ctx.register_middleware("llm_request", _optimize_request)


__all__ = [
    "ContextPolicy",
    "compact_skills_prompt",
    "compact_tools",
    "optimize_request",
    "register",
    "resolve_policy",
]
