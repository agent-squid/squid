"""Resolve (harness, provider) into a runnable execution config (ADR-0028).

Replaces the static `backends.py` Backend lookup. There is no `backends:`
config section anymore — a named agent selects harness + provider directly,
and this module combines the two into the same shape callers (runners.py,
topic_queue.py, journal.py, server.py) used to get from a `Backend`: a
resolved object exposing `execution_env()`, `driver_settings()`, `args`,
`protocol`, `interactive`, `fingerprint`, `available`, etc.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .harnesses import (
    DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS,
    default_provider_for,
    interactive_idle_timeout_seconds,
    harness_path,
    protocol_for,
    require_harness,
)
from .providers import Gauge, Provider, require_provider

LEGACY_BACKEND_AGENT_MAP: dict[str, tuple[str, Optional[str]]] = {
    "claude": ("claudecode", "anthropic"),
    "claude-live": ("claudecode", "anthropic"),
    "deepseek": ("claudecode", "deepseek"),
    "cc-deepseek": ("claudecode", "deepseek"),
    "deepcla": ("claudecode", "deepseek"),
    "codex": ("codex", "openai"),
    "cursor": ("cursor", "cursor"),
    "opencode": ("opencode", "opencode"),
    "pi": ("pi", "nvidia"),
}


def split_agent_ref(agent_ref: Optional[str], provider_id: Optional[str] = None) -> tuple[str, Optional[str]]:
    """Resolve a temporary/legacy agent backend field into harness + provider.

    Compatibility API values may be `harness` or `harness:provider`; old
    values such as `claude` and `deepseek` are mapped here for callers that
    still send or receive the runtime ref as `backend`.
    """
    if agent_ref and ":" in agent_ref:
        harness_id, parsed_provider = agent_ref.split(":", 1)
        return harness_id, provider_id or parsed_provider or None
    if agent_ref in LEGACY_BACKEND_AGENT_MAP:
        harness_id, mapped_provider = LEGACY_BACKEND_AGENT_MAP[agent_ref]
        return harness_id, provider_id or mapped_provider
    return agent_ref or "claudecode", provider_id


def agent_ref_for_storage(harness_id: str, provider_id: Optional[str] = None) -> str:
    return f"{harness_id}:{provider_id}" if provider_id else harness_id


@dataclass(frozen=True)
class InteractiveSettings:
    idle_timeout_seconds: float = DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS

    def public_dict(self) -> dict[str, Any]:
        return {"idle_timeout_seconds": self.idle_timeout_seconds}


@dataclass(frozen=True)
class ResolvedAgent:
    """A runnable (harness, provider) pair. Ephemeral — built on every
    resolution, never stored as named config (that's the point of ADR-0028)."""

    harness: str
    provider: Provider
    protocol: str
    interactive: InteractiveSettings = field(default_factory=InteractiveSettings)

    @property
    def id(self) -> str:
        """Display-only identifier for logs/errors, not a config key."""
        return f"{self.harness}:{self.provider.id}"

    @property
    def path(self) -> Optional[str]:
        return harness_path(self.harness)

    @property
    def color(self) -> str:
        return self.provider.color

    @property
    def label(self) -> str:
        return self.provider.label

    @property
    def gauge(self) -> Gauge:
        return self.provider.gauge

    @property
    def args(self) -> tuple[str, ...]:
        return self.provider.args

    @property
    def available(self) -> bool:
        return bool(self.path) and not self.missing_requirements()

    def missing_requirements(self) -> list[str]:
        return self.provider.missing_secrets()

    @property
    def fingerprint(self) -> str:
        payload = {
            "harness": self.harness,
            "provider": self.provider.id,
            "base_url": self.provider.base_url,
            "auth_type": self.provider.auth_type,
            "api_key": self.provider.api_key,
            "env": self.provider.env,
            "settings": self.provider.settings,
            "args": self.provider.args,
            "protocol": self.protocol,
            "interactive": self.interactive.public_dict(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()[:16]

    def execution_env(self) -> dict[str, str]:
        """Translate canonical provider connection fields into harness-native environment."""
        result = self.provider.resolved_env()
        api_key = self.provider.resolved_api_key()
        base_url = self.provider.base_url
        if self.harness == "claudecode":
            if base_url:
                result.setdefault("ANTHROPIC_BASE_URL", base_url)
            if api_key:
                result.setdefault("ANTHROPIC_AUTH_TOKEN", api_key)
        elif self.harness == "codex" and api_key:
            result.setdefault("SQUID_BACKEND_API_KEY", api_key)
        elif self.harness == "opencode" and api_key and self.provider.id == "deepseek":
            result.setdefault("DEEPSEEK_API_KEY", api_key)
        elif self.harness == "opencode" and api_key and self.provider.id == "nvidia":
            result.setdefault("NVIDIA_API_KEY", api_key)
        return result

    def driver_settings(self) -> dict[str, Any]:
        """Merge canonical provider fields into harness-native structured settings."""
        result = dict(self.provider.settings)
        if self.harness in ("pi", "opencode"):
            # Both CLIs resolve the provider from a "provider/model" pattern in
            # their model flag rather than a separate --provider argument.
            result.setdefault("provider", self.provider.id)
            return result
        if self.harness != "codex" or not self.provider.base_url:
            return result
        provider_key = re.sub(r"[^a-z0-9_]", "_", self.provider.id.lower())
        result.setdefault("model_provider", provider_key)
        providers = dict(result.get("model_providers") or {})
        entry = dict(providers.get(provider_key) or {})
        entry.setdefault("name", self.provider.id)
        entry.setdefault("base_url", self.provider.base_url)
        entry.setdefault("wire_api", "responses")
        if self.provider.api_key is not None:
            entry.setdefault("env_key", "SQUID_BACKEND_API_KEY")
        providers[provider_key] = entry
        result["model_providers"] = providers
        return result

    def public_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "provider": self.provider.id,
            "kind": self.provider.auth_type,
            "label": self.label,
            "color": self.color,
            "available": self.available,
            "path": self.path,
            "missing_secrets": self.missing_requirements(),
            "missing_requirements": self.missing_requirements(),
            "base_url": self.provider.base_url,
            "protocol": self.protocol,
            "interactive": self.interactive.public_dict(),
            "supports_idle_process": self.harness == "claudecode" and self.protocol == "interactive-cli",
            "gauge": self.gauge.public_dict(),
        }


def resolve_agent(harness_id: str, provider_id: Optional[str] = None,
                  protocol: Optional[str] = None) -> ResolvedAgent:
    require_harness(harness_id)
    provider = require_provider(provider_id or default_provider_for(harness_id))
    resolved_protocol = protocol_for(harness_id, protocol)
    interactive = (
        InteractiveSettings(interactive_idle_timeout_seconds(harness_id))
        if resolved_protocol.startswith("interactive-")
        else InteractiveSettings(0)
    )
    return ResolvedAgent(harness_id, provider, resolved_protocol, interactive)
