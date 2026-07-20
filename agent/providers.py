"""Providers — the `providers:` config section (ADR-0028).

A provider owns everything about an API endpoint that doesn't depend on
which harness is using it: label, color (quota-pool identity — see
ADR-0028's "color ties to provider" decision), base_url, auth (api_key or
subscription), gauge, models (a UI suggestion list, never server-validated),
and the env/settings/args escape hatches that used to live on backend
(ADR-0024).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import _cfg

SUPPORTED_AUTH_TYPES = frozenset({"api_key", "subscription"})
SUPPORTED_GAUGES = frozenset({"claude", "codex", "cursor", "deepseek", "kimi", "static", "none"})
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Zero-config providers only — nim/deepseek require a user-supplied api_key,
# so they're documented in config/squid.yaml.example rather than auto-seeded
# for legacy squid.yaml files that predate the `providers:` section.
_DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Claude", "color": "#AE5332",
        "auth": {"type": "subscription"}, "gauge": "claude",
        "supported_apis": ["/v1/messages"],
    },
    "openai": {
        "label": "GPT", "color": "#7070A0",
        "auth": {"type": "subscription"}, "gauge": "codex",
        "supported_apis": ["/v1/responses", "/v1/chat/completions"],
    },
    "cursor": {
        "label": "Cursor", "color": "#8F8D8D",
        "auth": {"type": "subscription"}, "gauge": "cursor",
        "supported_apis": ["/native/cursor"],
    },
    "opencode": {
        "label": "OpenCode", "color": "#CFCECD",
        "auth": {"type": "subscription"},
        "gauge": {"type": "static", "text": "Free tier", "title": "OpenCode-managed limits"},
        "supported_apis": ["/native/opencode"],
    },
}


@dataclass(frozen=True)
class Gauge:
    type: str = "none"
    text: Optional[str] = None
    title: Optional[str] = None

    def public_dict(self) -> dict[str, Any]:
        return {"type": self.type, "text": self.text, "title": self.title}


@dataclass(frozen=True)
class Provider:
    id: str
    label: str = ""
    color: str = "#888888"
    base_url: Optional[str] = None
    auth_type: str = "subscription"
    api_key: Any = None
    gauge: Gauge = field(default_factory=Gauge)
    models: tuple[str, ...] = ()
    supported_apis: frozenset[str] = field(default_factory=frozenset)
    env: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    args: tuple[str, ...] = ()

    def missing_secrets(self) -> list[str]:
        if self.auth_type != "api_key":
            return []
        missing: list[str] = []
        if not self.api_key:
            missing.append("api_key")
        elif isinstance(self.api_key, dict) and set(self.api_key) == {"env"}:
            name = self.api_key["env"]
            if not os.environ.get(name):
                missing.append(name)
        for value in self.env.values():
            if isinstance(value, dict) and set(value) == {"env"}:
                name = value["env"]
                if not os.environ.get(name):
                    missing.append(name)
        return missing

    def resolved_api_key(self) -> Optional[str]:
        if self.auth_type != "api_key" or self.api_key is None:
            return None
        if isinstance(self.api_key, dict):
            name = self.api_key["env"]
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"Provider {self.id!r} requires environment variable: {name}")
            return value
        return str(self.api_key)

    def resolved_env(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for name, value in self.env.items():
            if isinstance(value, dict):
                resolved = os.environ.get(value["env"])
                if not resolved:
                    raise ValueError(f"Provider {self.id!r} requires environment variable: {value['env']}")
                result[name] = resolved
            else:
                result[name] = str(value)
        return result

    def public_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "color": self.color,
            "base_url": self.base_url,
            "auth_type": self.auth_type,
            "missing_secrets": self.missing_secrets(),
            "gauge": self.gauge.public_dict(),
            "models": list(self.models),
            "supported_apis": sorted(self.supported_apis),
        }


def _validate_secret(provider_id: str, field_name: str, value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if (isinstance(value, dict) and set(value) == {"env"}
            and isinstance(value.get("env"), str) and _ENV_RE.fullmatch(value["env"])):
        return value
    raise ValueError(f"Provider {provider_id!r} {field_name} must be scalar or {{env: NAME}}")


def _validate_supported_apis(provider_id: str, value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not value:
        raise ValueError(f"Provider {provider_id!r} supported_apis must be a non-empty list")
    apis: list[str] = []
    for api in value:
        if not isinstance(api, str) or not api.strip():
            raise ValueError(f"Provider {provider_id!r} supported_apis must contain non-empty strings")
        apis.append(api.strip())
    return frozenset(apis)


def _validate_gauge(provider_id: str, raw: Any) -> Gauge:
    if raw is None:
        return Gauge()
    if isinstance(raw, str):
        if raw not in SUPPORTED_GAUGES - {"static"}:
            raise ValueError(f"Provider {provider_id!r} has unsupported gauge {raw!r}")
        return Gauge(type=raw)
    if not isinstance(raw, dict):
        raise ValueError(f"Provider {provider_id!r} gauge must be a string or mapping")
    gauge_type = raw.get("type")
    if gauge_type not in SUPPORTED_GAUGES:
        raise ValueError(f"Provider {provider_id!r} has unsupported gauge type {gauge_type!r}")
    text = raw.get("text")
    title = raw.get("title")
    if text is not None and not isinstance(text, str):
        raise ValueError(f"Provider {provider_id!r} gauge text must be a string")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"Provider {provider_id!r} gauge title must be a string")
    if gauge_type == "static" and not text:
        raise ValueError(f"Provider {provider_id!r} static gauge requires text")
    return Gauge(gauge_type, text, title)


def _validate_provider(provider_id: str, raw: Any) -> Provider:
    if not _ID_RE.fullmatch(provider_id):
        raise ValueError(f"Invalid provider id {provider_id!r}")
    if not isinstance(raw, dict):
        raise ValueError(f"Provider {provider_id!r} must be a mapping")

    label = raw.get("label", provider_id)
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"Provider {provider_id!r} label must be a non-empty string")

    color = raw.get("color", "#888888")
    if not isinstance(color, str) or not _COLOR_RE.fullmatch(color):
        raise ValueError(f"Provider {provider_id!r} color must be #RRGGBB")

    base_url = raw.get("base_url")
    if base_url is not None and (not isinstance(base_url, str) or not base_url):
        raise ValueError(f"Provider {provider_id!r} base_url must be a non-empty string")

    auth = raw.get("auth") or {"type": "subscription"}
    if not isinstance(auth, dict):
        raise ValueError(f"Provider {provider_id!r} auth must be a mapping")
    auth_type = auth.get("type")
    if auth_type not in SUPPORTED_AUTH_TYPES:
        raise ValueError(f"Provider {provider_id!r} has unsupported auth.type {auth_type!r}")
    api_key = _validate_secret(provider_id, "auth.api_key", auth.get("api_key"))
    if auth_type == "api_key" and not api_key:
        raise ValueError(f"Provider {provider_id!r} auth.type is api_key but auth.api_key is not set")

    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"Provider {provider_id!r} env must be a mapping")
    for name, value in env.items():
        if not isinstance(name, str) or not _ENV_RE.fullmatch(name):
            raise ValueError(f"Provider {provider_id!r} has invalid environment name {name!r}")
        if isinstance(value, dict):
            if set(value) != {"env"} or not isinstance(value.get("env"), str) or not _ENV_RE.fullmatch(value["env"]):
                raise ValueError(f"Provider {provider_id!r} environment reference for {name!r} must be {{env: NAME}}")
        elif not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Provider {provider_id!r} environment value for {name!r} must be scalar")

    settings = raw.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError(f"Provider {provider_id!r} settings must be a mapping")

    args = raw.get("args") or []
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError(f"Provider {provider_id!r} args must be a list of strings")

    models = raw.get("models") or []
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise ValueError(f"Provider {provider_id!r} models must be a list of strings")

    supported_apis = _validate_supported_apis(provider_id, raw.get("supported_apis"))
    gauge = _validate_gauge(provider_id, raw.get("gauge"))

    return Provider(provider_id, label, color.upper(), base_url, auth_type, api_key,
                    gauge, tuple(models), supported_apis, env, settings, tuple(args))


def _configured_providers() -> dict[str, Any]:
    configured = _cfg.get("providers")
    if configured is not None:
        if not isinstance(configured, dict) or not configured:
            raise ValueError("providers must be a non-empty mapping")
        return dict(configured)
    return {name: dict(value) for name, value in _DEFAULT_PROVIDERS.items()}


PROVIDERS: dict[str, Provider] = {
    provider_id: _validate_provider(provider_id, raw)
    for provider_id, raw in _configured_providers().items()
}


def get_provider(provider_id: str) -> Optional[Provider]:
    return PROVIDERS.get(provider_id)


def require_provider(provider_id: str) -> Provider:
    provider = get_provider(provider_id)
    if provider is None:
        raise ValueError(f"Unknown provider {provider_id!r}")
    return provider


def public_providers() -> dict[str, dict[str, Any]]:
    return {name: provider.public_dict() for name, provider in PROVIDERS.items()}
