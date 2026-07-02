"""Configured backend instances backed by one of Squid's coded CLI drivers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import (
    CLAUDE_PATH,
    CODEX_PATH,
    CURSOR_PATH,
    OPENCODE_PATH,
    _cfg,
)


SUPPORTED_DRIVERS = frozenset({"claude", "codex", "cursor", "opencode"})
SUPPORTED_PROTOCOLS = frozenset({"oneshot-cli", "interactive-cli", "interactive-pty"})
DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS = 3600
SUPPORTED_PROTOCOLS_BY_DRIVER = {
    "claude": frozenset({"oneshot-cli", "interactive-cli"}),
    "codex": frozenset({"oneshot-cli", "interactive-cli"}),
    "cursor": frozenset({"oneshot-cli", "interactive-cli"}),
    "opencode": frozenset({"oneshot-cli", "interactive-cli"}),
}
DEFAULT_PROTOCOL_BY_DRIVER = {
    "claude": "interactive-cli",
    "codex": "interactive-cli",
    "cursor": "interactive-cli",
    "opencode": "interactive-cli",
}
SUPPORTED_GAUGES = frozenset({"claude", "codex", "cursor", "deepseek", "static", "none"})
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DRIVER_PATHS = {
    "claude": CLAUDE_PATH,
    "codex": CODEX_PATH,
    "cursor": CURSOR_PATH,
    "opencode": OPENCODE_PATH,
}

_DEFAULT_BACKENDS: dict[str, dict[str, Any]] = {
    "claude": {"driver": "claude", "label": "Claude", "color": "#AE5332", "gauge": "claude"},
    "codex": {"driver": "codex", "label": "Codex", "color": "#7070A0", "gauge": "codex"},
    "cursor": {"driver": "cursor", "label": "Cursor", "color": "#FFFFFF", "gauge": "cursor"},
    "opencode": {
        "driver": "opencode", "label": "OpenCode", "color": "#CFCECD",
        "gauge": {"type": "static", "text": "Free tier", "title": "OpenCode-managed limits"},
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
class InteractiveSettings:
    idle_timeout_seconds: float = DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS

    def public_dict(self) -> dict[str, Any]:
        return {"idle_timeout_seconds": self.idle_timeout_seconds}


@dataclass(frozen=True)
class Backend:
    id: str
    driver: str
    color: str = "#888888"
    label: str = ""
    env: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    args: tuple[str, ...] = ()
    provider: Optional[str] = None
    api_key: Any = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    protocol: str = "oneshot-cli"
    interactive: InteractiveSettings = field(default_factory=InteractiveSettings)
    gauge: Gauge = field(default_factory=Gauge)

    @property
    def path(self) -> Optional[str]:
        return _DRIVER_PATHS.get(self.driver)

    @property
    def available(self) -> bool:
        return bool(self.path) and not self.missing_secrets()

    @property
    def requires_api_key(self) -> bool:
        return self.provider == "deepseek" or self.gauge.type == "deepseek"

    @property
    def fingerprint(self) -> str:
        payload = {
            "driver": self.driver,
            "env": self.env,
            "settings": self.settings,
            "args": self.args,
            "provider": self.provider,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "protocol": self.protocol,
            "interactive": self.interactive.public_dict(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()[:16]

    def missing_secrets(self) -> list[str]:
        missing: list[str] = []
        if self.requires_api_key and not self.api_key:
            missing.append("api_key")
        for value in self.env.values():
            if isinstance(value, dict) and set(value) == {"env"}:
                name = value["env"]
                if not os.environ.get(name):
                    missing.append(name)
        if isinstance(self.api_key, dict) and set(self.api_key) == {"env"}:
            name = self.api_key["env"]
            if not os.environ.get(name):
                missing.append(name)
        return missing

    def resolved_api_key(self) -> Optional[str]:
        if self.api_key is None:
            return None
        if isinstance(self.api_key, dict):
            name = self.api_key["env"]
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"Backend {self.id!r} requires environment variable: {name}")
            return value
        return str(self.api_key)

    def resolved_env(self) -> dict[str, str]:
        result: dict[str, str] = {}
        missing = self.missing_secrets()
        if missing:
            raise ValueError(f"Backend {self.id!r} requires environment variable(s): {', '.join(missing)}")
        for name, value in self.env.items():
            if isinstance(value, dict):
                result[name] = os.environ[value["env"]]
            else:
                result[name] = str(value)
        return result

    def execution_env(self) -> dict[str, str]:
        """Translate canonical connection fields into driver-specific environment."""
        result = self.resolved_env()
        api_key = self.resolved_api_key()
        if self.driver == "claude":
            if self.base_url:
                result.setdefault("ANTHROPIC_BASE_URL", self.base_url)
            if api_key:
                result.setdefault("ANTHROPIC_AUTH_TOKEN", api_key)
        elif self.driver == "codex" and api_key:
            result.setdefault("SQUID_BACKEND_API_KEY", api_key)
        elif self.driver == "opencode" and api_key and self.provider == "deepseek":
            result.setdefault("DEEPSEEK_API_KEY", api_key)
        return result

    def driver_settings(self) -> dict[str, Any]:
        """Merge canonical connection fields into driver-native structured settings."""
        result = dict(self.settings)
        if self.driver != "codex" or not self.base_url:
            return result
        provider_id = re.sub(r"[^a-z0-9_]", "_", (self.provider or self.id).lower())
        result.setdefault("model_provider", provider_id)
        providers = dict(result.get("model_providers") or {})
        provider = dict(providers.get(provider_id) or {})
        provider.setdefault("name", self.provider or self.id)
        provider.setdefault("base_url", self.base_url)
        provider.setdefault("wire_api", "responses")
        if self.api_key is not None:
            provider.setdefault("env_key", "SQUID_BACKEND_API_KEY")
        providers[provider_id] = provider
        result["model_providers"] = providers
        return result

    def public_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "label": self.label,
            "color": self.color,
            "available": self.available,
            "path": self.path,
            "missing_secrets": self.missing_secrets(),
            "provider": self.provider,
            "base_url": self.base_url,
            "protocol": self.protocol,
            "interactive": self.interactive.public_dict(),
            "gauge": self.gauge.public_dict(),
        }


def _validate_secret(backend_id: str, field_name: str, value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if (isinstance(value, dict) and set(value) == {"env"}
            and isinstance(value.get("env"), str) and _ENV_RE.fullmatch(value["env"])):
        return value
    raise ValueError(f"Backend {backend_id!r} {field_name} must be scalar or {{env: NAME}}")


def _validate_gauge(backend_id: str, raw: Any) -> Gauge:
    if raw is None:
        return Gauge()
    if isinstance(raw, str):
        if raw not in SUPPORTED_GAUGES - {"static"}:
            raise ValueError(f"Backend {backend_id!r} has unsupported gauge {raw!r}")
        return Gauge(type=raw)
    if not isinstance(raw, dict):
        raise ValueError(f"Backend {backend_id!r} gauge must be a string or mapping")
    gauge_type = raw.get("type")
    if gauge_type not in SUPPORTED_GAUGES:
        raise ValueError(f"Backend {backend_id!r} has unsupported gauge type {gauge_type!r}")
    text = raw.get("text")
    title = raw.get("title")
    if text is not None and not isinstance(text, str):
        raise ValueError(f"Backend {backend_id!r} gauge text must be a string")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"Backend {backend_id!r} gauge title must be a string")
    if gauge_type == "static" and not text:
        raise ValueError(f"Backend {backend_id!r} static gauge requires text")
    return Gauge(gauge_type, text, title)


def _validate_interactive(backend_id: str, raw: Any) -> InteractiveSettings:
    if raw is None:
        return InteractiveSettings()
    if not isinstance(raw, dict):
        raise ValueError(f"Backend {backend_id!r} interactive must be a mapping")
    timeout = raw.get("idle_timeout_seconds", DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
        raise ValueError(f"Backend {backend_id!r} interactive.idle_timeout_seconds must be a non-negative number")
    return InteractiveSettings(float(timeout))


def _validate_backend(backend_id: str, raw: Any) -> Backend:
    if not _ID_RE.fullmatch(backend_id):
        raise ValueError(f"Invalid backend id {backend_id!r}")
    if not isinstance(raw, dict):
        raise ValueError(f"Backend {backend_id!r} must be a mapping")
    driver = raw.get("driver")
    if driver not in SUPPORTED_DRIVERS:
        raise ValueError(f"Backend {backend_id!r} has unsupported driver {driver!r}")
    protocol = raw.get("protocol") or DEFAULT_PROTOCOL_BY_DRIVER[driver]
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"Backend {backend_id!r} has unsupported protocol {protocol!r}")
    if protocol not in SUPPORTED_PROTOCOLS_BY_DRIVER[driver]:
        raise ValueError(f"Backend {backend_id!r} protocol {protocol!r} is not supported by driver {driver!r}")
    color = raw.get("color", "#888888")
    if not isinstance(color, str) or not _COLOR_RE.fullmatch(color):
        raise ValueError(f"Backend {backend_id!r} color must be #RRGGBB")
    label = raw.get("label", backend_id)
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"Backend {backend_id!r} label must be a non-empty string")
    env = raw.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"Backend {backend_id!r} env must be a mapping")
    for name, value in env.items():
        if not isinstance(name, str) or not _ENV_RE.fullmatch(name):
            raise ValueError(f"Backend {backend_id!r} has invalid environment name {name!r}")
        if isinstance(value, dict):
            if set(value) != {"env"} or not isinstance(value.get("env"), str) or not _ENV_RE.fullmatch(value["env"]):
                raise ValueError(f"Backend {backend_id!r} environment reference for {name!r} must be {{env: NAME}}")
        elif not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Backend {backend_id!r} environment value for {name!r} must be scalar")
    settings = raw.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError(f"Backend {backend_id!r} settings must be a mapping")
    args = raw.get("args") or []
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError(f"Backend {backend_id!r} args must be a list of strings")
    provider = raw.get("provider")
    if provider is not None and (not isinstance(provider, str) or not provider):
        raise ValueError(f"Backend {backend_id!r} provider must be a non-empty string")
    base_url = raw.get("base_url")
    if base_url is not None and (not isinstance(base_url, str) or not base_url):
        raise ValueError(f"Backend {backend_id!r} base_url must be a non-empty string")
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError(f"Backend {backend_id!r} model must be a non-empty string")
    api_key = _validate_secret(backend_id, "api_key", raw.get("api_key"))
    gauge = _validate_gauge(backend_id, raw.get("gauge"))
    interactive = _validate_interactive(backend_id, raw.get("interactive"))
    return Backend(backend_id, driver, color.upper(), label, env, settings, tuple(args),
                   provider, api_key, base_url, model, protocol, interactive, gauge)


def _configured_backends() -> dict[str, Any]:
    configured = _cfg.get("backends")
    if configured is not None:
        if not isinstance(configured, dict) or not configured:
            raise ValueError("backends must be a non-empty mapping")
        return configured

    # Compatibility for existing squid.yaml files. New installations receive an
    # explicit backends section from config/squid.yaml.example.
    defaults = {name: dict(value) for name, value in _DEFAULT_BACKENDS.items()}
    legacy = _cfg.get("deepseek") or {}
    if legacy.get("claude_key"):
        defaults["deepseek"] = {
            "driver": "claude",
            "label": "DeepSeek",
            "color": "#4D9DE0",
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key": legacy["claude_key"],
            "gauge": "deepseek",
        }
    return defaults


BACKENDS: dict[str, Backend] = {
    backend_id: _validate_backend(backend_id, raw)
    for backend_id, raw in _configured_backends().items()
}


def get_backend(backend_id: str) -> Optional[Backend]:
    return BACKENDS.get(backend_id)


def require_backend(backend_id: str) -> Backend:
    backend = get_backend(backend_id)
    if backend is None:
        raise ValueError(f"Unknown backend {backend_id!r}")
    return backend


def public_backends() -> dict[str, dict[str, Any]]:
    return {name: backend.public_dict() for name, backend in BACKENDS.items()}
