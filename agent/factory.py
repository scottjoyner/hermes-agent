"""Single construction path for AIAgent instances.

Historically the agent constructor was called inline in two places with
divergent kwargs (``cli.py`` and ``gateway/run.py`` -- see LLD W-79). This
module centralizes construction so that both call sites build their agent
through :func:`make_agent`, keeping behavior identical while giving us one
place to apply shared defaults, validation, and (eventually) the
correlation-envelope shim required by the shared contract package.

The real ``AIAgent`` class lives in ``run_agent.py`` (which has heavy/order
sensitive imports). To preserve the lazy-import discipline already used in
``cli.py`` we import it inside the factory rather than at module top.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _load_ai_agent():
    """Resolve the real AIAgent class lazily (mirrors cli.py's wrapper)."""
    from run_agent import AIAgent as _AIAgent

    return _AIAgent


def make_agent(
    config: Optional[Dict[str, Any]] = None,
    platform: Optional[str] = None,
    **overrides: Any,
) -> Any:
    """Construct an ``AIAgent`` through ONE path used by both CLI and gateway.

    Args:
        config: Optional resolved config dict. Reserved for future shared
            default extraction; the factory does not currently require it but
            accepts it so call sites can pass their already-resolved config.
        platform: Platform tag forwarded to ``AIAgent`` (e.g. ``"cli"``,
            ``"telegram"``). Passed through to ``AIAgent(platform=...)``.
        **overrides: Any keyword arguments accepted by ``AIAgent.__init__``.
            These are forwarded verbatim so behavior is unchanged vs. the
            previous inline construction.

    Returns:
        A fully constructed ``AIAgent`` instance.
    """
    if platform is not None and "platform" not in overrides:
        overrides["platform"] = platform

    _AIAgent = _load_ai_agent()
    return _AIAgent(**overrides)
