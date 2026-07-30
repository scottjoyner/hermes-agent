"""External fleet gateway mode for AssistX/auto-router deployments.

In this mode Hermes does not own a node registry, health model, concurrency
counter, access-path selector, or physical model placement. It talks to one
OpenAI-compatible gateway for inference and reads authenticated operator state
from that gateway for status and diagnostics only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from hermes_cli.urllib_security import open_credentialed_url

from .core import is_private_endpoint, join_endpoint, mapping, normalize_base_url


_ALLOWED_MODES = {"external", "standalone", "disabled"}


@dataclass(frozen=True)
class ExternalFleetConfig:
    base_url: str
    admin_url: str
    api_key_env: str = ""
    admin_token_env: str = ""
    default_model: str = "auto/code"
    strict_offline: bool = True
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class FleetOperatingConfig:
    mode: str
    external: ExternalFleetConfig | None = None


def _floating(value: Any, default: float, minimum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result >= minimum else default


def parse_operating_config(root: Mapping[str, Any]) -> FleetOperatingConfig:
    section = mapping(root.get("fleet"))
    raw_mode = str(section.get("mode") or "").strip().lower()
    if not raw_mode:
        # Backward compatibility for the original PR #10 standalone shape.
        raw_mode = "standalone" if section.get("enabled") or section.get("nodes") else "disabled"
    if raw_mode not in _ALLOWED_MODES:
        raise ValueError(
            f"fleet.mode must be one of {sorted(_ALLOWED_MODES)}, got {raw_mode!r}"
        )

    if raw_mode != "external":
        return FleetOperatingConfig(mode=raw_mode)

    if section.get("nodes"):
        raise ValueError("fleet.nodes is forbidden when fleet.mode is external")
    external = mapping(section.get("external"))
    base_url = normalize_base_url(external.get("base_url"))
    admin_url = normalize_base_url(external.get("admin_url"))
    strict_offline = bool(external.get("strict_offline", True))
    if not base_url:
        raise ValueError("fleet.external.base_url is required in external mode")
    if not admin_url:
        admin_url = base_url.removesuffix("/v1")
    if strict_offline:
        for label, value in (("base_url", base_url), ("admin_url", admin_url)):
            if not is_private_endpoint(value):
                raise ValueError(
                    f"fleet.external.{label} must be private when strict_offline is true"
                )
    return FleetOperatingConfig(
        mode="external",
        external=ExternalFleetConfig(
            base_url=base_url,
            admin_url=admin_url,
            api_key_env=str(external.get("api_key_env") or "").strip(),
            admin_token_env=str(external.get("admin_token_env") or "").strip(),
            default_model=str(external.get("default_model") or "auto/code").strip(),
            strict_offline=strict_offline,
            timeout_seconds=min(
                120.0,
                max(0.5, _floating(external.get("timeout_seconds"), 10.0, 0.5)),
            ),
        ),
    )


class ExternalFleetClient:
    """Read-only operator client for an AssistX-authorized auto-router gateway."""

    def __init__(self, config: ExternalFleetConfig):
        self.config = config

    def _headers(self, *, admin: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "hermes-fleet-external/0.1",
        }
        if self.config.api_key_env:
            token = os.getenv(self.config.api_key_env, "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if admin and self.config.admin_token_env:
            token = os.getenv(self.config.admin_token_env, "").strip()
            if token:
                headers["X-Admin-Token"] = token
        return headers

    def _get(self, url: str, *, admin: bool = False) -> tuple[int, Any]:
        request = urllib.request.Request(url, headers=self._headers(admin=admin), method="GET")
        try:
            with open_credentialed_url(
                request,
                timeout=self.config.timeout_seconds,
            ) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body) if body else {}
                return int(getattr(response, "status", 200) or 200), payload
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {"error": body[:1000]}
            return int(exc.code), payload
        except Exception as exc:
            return 0, {"error": f"{type(exc).__name__}: {exc}"}

    def status(self) -> dict[str, Any]:
        health_code, health = self._get(join_endpoint(self.config.admin_url, "/health"))
        models_code, models = self._get(join_endpoint(self.config.base_url, "/v1/models"))
        admission_code, admission = self._get(
            join_endpoint(self.config.admin_url, "/admin/admission"),
            admin=True,
        )
        errors: list[str] = []
        if health_code != 200:
            errors.append(f"router health returned {health_code or 'unreachable'}")
        if models_code != 200:
            errors.append(f"router models returned {models_code or 'unreachable'}")
        if admission_code != 200:
            errors.append(
                f"router admission returned {admission_code or 'unreachable'}; "
                "check fleet.external.admin_token_env"
            )
        model_items = models.get("data") if isinstance(models, Mapping) else []
        if not isinstance(model_items, list):
            model_items = []
        admission_payload = admission if isinstance(admission, Mapping) else {}
        return {
            "mode": "external",
            "authority": "assistx/auto-router",
            "routing_owner": "auto-router",
            "inventory_owner": "AssistX/Neo4j",
            "hermes_fleet_proxy_enabled": False,
            "physical_endpoint_discovery": False,
            "local_concurrency_authority": False,
            "base_url": self.config.base_url,
            "admin_url": self.config.admin_url,
            "default_model": self.config.default_model,
            "strict_offline": self.config.strict_offline,
            "healthy": not errors,
            "models": model_items,
            "admission": admission_payload,
            "errors": errors,
            "http_status": {
                "health": health_code,
                "models": models_code,
                "admission": admission_code,
            },
            "router_health": health if isinstance(health, Mapping) else {},
        }

    def doctor(self) -> dict[str, Any]:
        status = self.status()
        errors = list(status.get("errors") or [])
        if self.config.strict_offline:
            for label, value in (
                ("base_url", self.config.base_url),
                ("admin_url", self.config.admin_url),
            ):
                if not is_private_endpoint(value):
                    errors.append(f"{label} is not private")
        if self.config.admin_token_env and not os.getenv(
            self.config.admin_token_env,
            "",
        ).strip():
            errors.append(
                f"admin token environment variable {self.config.admin_token_env} is empty"
            )
        status["errors"] = errors
        status["ok"] = not errors
        return status

    def route_intent(self, model: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        selected = str(model or self.config.default_model).strip()
        return {
            "mode": "external",
            "model_intent": selected,
            "gateway": self.config.base_url,
            "routing_owner": "auto-router",
            "physical_runtime_selected_by_hermes": False,
            "metadata": dict(metadata or {}),
            "note": (
                "Hermes supplies a semantic auto/* alias and task metadata. "
                "auto-router performs policy selection and physical runtime admission."
            ),
        }

    def config_dict(self) -> dict[str, Any]:
        return asdict(self.config)
