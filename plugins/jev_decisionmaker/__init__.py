"""JEV (Judge-Evaluate-Verify) decisionmaker plugin for Hermes.

After each agent turn, a small fast judge model evaluates the output
and decides: accept, retry, or delegate to a different model.

Configuration (hermes config):
  jev.enabled: true
  jev.judge.model: local/destroyer-1b
  jev.judge.provider: custom
  jev.judge.base_url: http://destroyer.tailcb8954.ts.net:1238/v1
  jev.generator.model: local/destroyer-36b
  jev.delegation_threshold: 0.5
  jev.max_retries: 2
  jev.swap_on_delegation: true

Verdicts:
  accept   — generator output is good, pass through
  retry    — generator output needs work, re-prompt same model
  delegate — route to a different model (swap active model)

The judge runs on K2-1B (12.8 tok/s) so the overhead is negligible.
Fail-open: judge errors never block progress.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger("jev_decisionmaker")

JEV_SYSTEM_PROMPT = (
    "You are a JEV judge. Evaluate the response against the goal. "
    "Return ONLY this exact JSON, no markdown, no thinking: "
    "{\"verdict\":\"accept\"|\"retry\"|\"delegate\",\"score\":0.0-1.0,\"reason\":\"<10 words>\",\"suggested_model\":\"<model>\"}."
)

DEFAULT_CONFIG = {
    "enabled": False,
    "judge": {
        "model": "local/destroyer-1b",
        "provider": "custom",
        "base_url": "http://destroyer.tailcb8954.ts.net:1238/v1",
        "max_tokens": 128,
        "temperature": 0.0,
    },
    "generator": {
        "model": "local/destroyer-36b",
        "provider": "custom",
        "base_url": "http://destroyer.tailcb8954.ts.net:1235/v1",
    },
    "delegation_threshold": 0.5,
    "max_retries": 2,
    "swap_on_delegation": True,
    "verbose": False,
}

_JEV_CONFIG_PATH = "/home/scott/fleet-data/jev-config.json"


def _load_config() -> dict:
    """Load JEV config from file, falling back to defaults."""
    try:
        with open(_JEV_CONFIG_PATH) as f:
            data = json.load(f)
        return {**DEFAULT_CONFIG, **(data.get("jev") or {})}
    except Exception:
        return DEFAULT_CONFIG.copy()


def _chat_completion(base_url: str, messages: list, model: str, max_tokens: int, temperature: float) -> str:
    """Call an OpenAI-compatible chat completions endpoint directly."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    msg = data["choices"][0]["message"]
    return msg.get("content", "") or msg.get("reasoning_content", "") or ""


class JEVDecisionmaker:
    """JEV decisionmaker — evaluates agent output and decides routing."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**_load_config(), **(config or {})}
        self._judge_cfg = {**DEFAULT_CONFIG["judge"], **(self.config.get("judge") or {})}
        self._generator_cfg = {
            **DEFAULT_CONFIG["generator"],
            **(self.config.get("generator") or {}),
        }
        self._retry_count = 0
        self._active_model = self._generator_cfg["model"]

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def evaluate(
        self, goal: str, response: str, context: str | None = None
    ) -> dict[str, Any]:
        """Judge the agent's response against the goal.

        Returns dict with keys: verdict, score, reason, suggested_model.
        Fail-open: returns accept on any error.
        """
        if not self.enabled:
            return {"verdict": "accept", "score": 1.0, "reason": "jev disabled", "suggested_model": None}

        user_prompt = self._build_prompt(goal, response, context)
        messages = [
            {"role": "system", "content": JEV_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            text = _chat_completion(
                base_url=self._judge_cfg["base_url"],
                messages=messages,
                model=self._judge_cfg["model"],
                max_tokens=self._judge_cfg.get("max_tokens", 256),
                temperature=self._judge_cfg.get("temperature", 0.0),
            )
            return self._parse_verdict(text)
        except Exception as exc:
            logger.warning("jev judge call failed: %s", exc)
            return {"verdict": "accept", "score": 1.0, "reason": f"judge error: {exc}", "suggested_model": None}

    def _build_prompt(self, goal: str, response: str, context: str | None) -> str:
        parts = [
            f"GOAL: {goal}",
            f"RESPONSE: {response}",
            "Return ONLY JSON: {\"verdict\":\"accept\"|\"retry\"|\"delegate\",\"score\":0.0-1.0,\"reason\":\"<10 words\",\"suggested_model\":\"<model>\"}",
        ]
        if context:
            parts.append(f"CONTEXT: {context}")
        return "\n".join(parts)

    def _parse_verdict(self, text: str) -> dict[str, Any]:
        # Extract first JSON block from prose
        m = re.search(r"\{[^}]+\}", text, re.DOTALL)
        if m:
            text = m.group(0)
        # Try to fix truncated JSON by finding the last valid closing brace
        text = text.strip()
        text = re.sub(r"```json?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"```\s*$", "", text)
        # Try parsing as-is, then try fixing truncated JSON
        for attempt_text in [text, text + "}"]:
            try:
                data = json.loads(attempt_text)
                break
            except json.JSONDecodeError:
                continue
        else:
            return {"verdict": "accept", "score": 1.0, "reason": f"judge parse error: {text[:80]}", "suggested_model": None}
        verdict = data.get("verdict", "accept")
        if verdict not in {"accept", "retry", "delegate"}:
            verdict = "accept"
        return {
            "verdict": verdict,
            "score": float(data.get("score", 1.0)),
            "reason": data.get("reason", ""),
            "suggested_model": data.get("suggested_model"),
        }

    def should_retry(self, verdict: dict[str, Any]) -> bool:
        """Return True if the verdict says retry and we haven't exceeded max_retries."""
        if verdict["verdict"] != "retry":
            return False
        if self._retry_count >= self.config.get("max_retries", 2):
            return False
        self._retry_count += 1
        return True

    def should_delegate(self, verdict: dict[str, Any]) -> bool:
        """Return True if the score is below threshold and a suggested model is given."""
        if verdict["verdict"] != "delegate":
            return False
        score = verdict.get("score", 1.0)
        threshold = self.config.get("delegation_threshold", 0.5)
        return score < threshold and bool(verdict.get("suggested_model"))

    def delegate_model(self, verdict: dict[str, Any]) -> str | None:
        """Return the model to delegate to, or None."""
        if not self.should_delegate(verdict):
            return None
        model = verdict["suggested_model"]
        if self.config.get("swap_on_delegation", True):
            self._active_model = model
            self._retry_count = 0
        return model

    def reset(self) -> None:
        """Reset retry counter for a new goal/turn."""
        self._retry_count = 0
        self._active_model = self._generator_cfg["model"]


jev = JEVDecisionmaker()


class JEVProviderProfile(ProviderProfile):
    """JEV decisionmaker provider — not a real LLM provider, just a config marker."""

    def build_api_kwargs_extras(self, **ctx):
        return {}, {}

    def fetch_models(self, **ctx):
        return ["local/destroyer-1b", "local/destroyer-36b"]


jev_profile = JEVProviderProfile(
    name="jev",
    aliases=("jev-decisionmaker", "judge"),
    display_name="JEV Decisionmaker",
    description="JEV (Judge-Evaluate-Verify) routing: fast judge decides accept/retry/delegate",
    env_vars=(),
    base_url="",
    default_max_tokens=256,
)

register_provider(jev_profile)