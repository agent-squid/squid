"""Resolve (harness, provider) into a runnable execution config (ADR-0028).

Replaces the static `backends.py` Backend lookup. There is no `backends:`
config section anymore — a named agent selects harness + provider directly,
and this module combines the two into the same shape callers (runners.py,
topic_queue.py, journal.py, server.py) used to get from a `Backend`: a
resolved object exposing `execution_env()`, `harness_settings()`, `args`,
`protocol`, `interactive`, `fingerprint`, `available`, etc.
"""

from __future__ import annotations

import hashlib
import json
import os
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

# Providers pi/opencode natively recognize — no base_url forwarding needed.
_STANDARD_PROVIDERS = frozenset({"openai", "anthropic", "opencode", "nvidia"})

# pi ignores OPENAI_BASE_URL / ANTHROPIC_BASE_URL env vars for providers it
# doesn't natively know — custom providers must be declared in
# ~/.pi/agent/models.json instead (models-store.json is pi's own cache of
# built-in catalogs, not a user-writable config file).
PI_MODELS_FILE = os.path.expanduser("~/.pi/agent/models.json")


def _pi_api_key_env_var(provider_id: str) -> str:
    """Env var name pi should read a custom provider's key from.

    Keyed off the squid provider id (not the detected openai/anthropic
    family) so two different custom providers detected as the same wire
    protocol never collide on one env var."""
    return f"SQUID_PI_{re.sub(r'[^A-Z0-9_]', '_', provider_id.upper())}_API_KEY"


def _pi_api_type(apis: frozenset[str], *, has_api_key: bool = False) -> str:
    """Map squid supported_apis to a pi models.json ``api`` value.

    For ``/v1/responses`` pi distinguishes between API-key auth
    (``openai-responses``) and OAuth / subscription auth
    (``openai-codex-responses``)."""
    if "/v1/messages" in apis:
        return "anthropic-messages"
    if "/v1/responses" in apis:
        return "openai-responses" if has_api_key else "openai-codex-responses"
    return "openai-completions"


def _opencode_api_key_env_var(provider_id: str) -> str:
    """Env var name opencode should read a custom provider's key from.

    Same rationale as ``_pi_api_key_env_var``: keyed off the squid provider
    id so two different custom providers never collide on one env var."""
    return f"SQUID_OPENCODE_{re.sub(r'[^A-Z0-9_]', '_', provider_id.upper())}_API_KEY"


def _opencode_npm_package(apis: frozenset[str]) -> str:
    """Map squid supported_apis to the ai-sdk package opencode's provider
    config's ``npm`` field should load for a custom provider."""
    if "/v1/messages" in apis:
        return "@ai-sdk/anthropic"
    if "/v1/responses" in apis:
        return "@ai-sdk/openai"
    return "@ai-sdk/openai-compatible"


def _load_pi_models_file() -> dict[str, Any]:
    if not os.path.exists(PI_MODELS_FILE):
        return {}
    try:
        with open(PI_MODELS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_pi_models_file(doc: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(PI_MODELS_FILE), exist_ok=True)
    with open(PI_MODELS_FILE, "w") as f:
        json.dump(doc, f, indent=2)


def sync_pi_models_store(*, provider_id: str, base_url: str,
                         supported_apis: frozenset[str], label: str,
                         has_api_key: bool, model: Optional[str] = None) -> None:
    """Register a custom provider in ``~/.pi/agent/models.json``.

    pi only recognizes ``baseUrl`` from a provider declared in this file —
    it never reads base URLs from env vars for providers it doesn't natively
    know. The API key is written as an env var *reference* (``$NAME``), never
    the resolved secret, so the on-disk file never holds a raw credential;
    ``execution_env()`` sets that same env var name for the pi subprocess.
    Call this when a pi agent is created or updated with a non-standard
    provider.
    """
    api_type = _pi_api_type(supported_apis, has_api_key=has_api_key)
    model_id = model or provider_id
    api_key_ref = f"${_pi_api_key_env_var(provider_id)}" if has_api_key else None

    doc = _load_pi_models_file()
    providers: dict[str, Any] = dict(doc.get("providers") or {})
    provider_entry: dict[str, Any] = dict(providers.get(provider_id) or {})
    models: list[dict[str, Any]] = list(provider_entry.get("models") or [])

    unchanged = (
        provider_entry.get("baseUrl") == base_url
        and provider_entry.get("api") == api_type
        and provider_entry.get("apiKey") == api_key_ref
        and any(m.get("id") == model_id for m in models)
    )
    if unchanged:
        return

    if not any(m.get("id") == model_id for m in models):
        models.append({"id": model_id, "name": f"{label} ({model_id})"})

    provider_entry["baseUrl"] = base_url
    provider_entry["api"] = api_type
    if api_key_ref:
        provider_entry["apiKey"] = api_key_ref
    else:
        provider_entry.pop("apiKey", None)
    provider_entry["models"] = models

    providers[provider_id] = provider_entry
    doc["providers"] = providers
    _write_pi_models_file(doc)


def remove_pi_models_store(provider_id: str, model: Optional[str] = None) -> None:
    """Remove a provider/model entry from pi's models.json.

    If *model* is given only that model is removed; otherwise the entire
    provider is dropped.  Safe to call when the file doesn't exist."""
    doc = _load_pi_models_file()
    providers: dict[str, Any] = dict(doc.get("providers") or {})
    if provider_id not in providers:
        return

    if model:
        entry = providers[provider_id]
        entry["models"] = [m for m in entry.get("models", []) if m.get("id") != model]
        if entry["models"]:
            providers[provider_id] = entry
        else:
            del providers[provider_id]
    else:
        del providers[provider_id]

    doc["providers"] = providers
    _write_pi_models_file(doc)


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

    def sync_pi_provider(self, model: Optional[str] = None) -> None:
        """Ensure pi's models.json has an entry for this provider.

        Standard providers (nvidia, openai, anthropic, opencode) are already
        in pi's own built-in catalog — skip them so we never shadow pi's
        real base URL/model list with a squid-side guess."""
        if self.harness != "pi" or self.provider.id in _STANDARD_PROVIDERS:
            return
        sync_pi_models_store(
            provider_id=self.provider.id,
            base_url=self.provider.base_url,
            supported_apis=self.provider.supported_apis,
            label=self.provider.label,
            has_api_key=bool(self.provider.resolved_api_key()),
            model=model,
        )

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

    def execution_env(self, model: Optional[str] = None) -> dict[str, str]:
        """Translate canonical provider connection fields into harness-native environment.

        *model* is only used to populate the per-launch custom-provider
        declaration for opencode's ``OPENCODE_CONFIG_CONTENT`` — every other
        branch ignores it."""
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
        elif self.harness == "pi" and api_key:
            if self.provider.id in _STANDARD_PROVIDERS:
                # pi's own native env var convention for a provider it
                # recognizes directly (e.g. NVIDIA_API_KEY).
                result.setdefault(f"{self.provider.id.upper()}_API_KEY", api_key)
            else:
                # Must match the "$NAME" reference sync_pi_models_store()
                # writes into models.json's apiKey field for this provider.
                result.setdefault(_pi_api_key_env_var(self.provider.id), api_key)
        elif self.harness == "opencode":
            if self.provider.id in _STANDARD_PROVIDERS:
                # opencode's own native env var convention for a provider it
                # recognizes directly (e.g. OPENAI_API_KEY).
                if api_key:
                    result.setdefault(f"{self.provider.id.upper()}_API_KEY", api_key)
            else:
                # opencode ignores env var base URLs entirely — it only
                # recognizes providers declared in opencode.json or, per-launch,
                # via OPENCODE_CONFIG_CONTENT (verified against the installed
                # opencode binary: this is parsed as JSON and merged with the
                # highest precedence, after global/project config). The key
                # is passed as an {env:NAME} reference, never inlined, so it
                # only ever lives in this env var, not in any config content.
                env_var = _opencode_api_key_env_var(self.provider.id)
                options: dict[str, Any] = {}
                if base_url:
                    options["baseURL"] = base_url
                if api_key:
                    result.setdefault(env_var, api_key)
                    options["apiKey"] = f"{{env:{env_var}}}"
                config_content = {
                    "provider": {
                        self.provider.id: {
                            "npm": _opencode_npm_package(self.provider.supported_apis),
                            "name": self.provider.label,
                            "options": options,
                            "models": {(model or self.provider.id): {}},
                        }
                    }
                }
                result.setdefault("OPENCODE_CONFIG_CONTENT", json.dumps(config_content))
        return result

    def harness_settings(self) -> dict[str, Any]:
        """Merge canonical provider fields into harness-native structured settings."""
        result = dict(self.provider.settings)
        if self.harness == "pi":
            # pi ignores env var base URLs for providers it doesn't natively
            # know — we sync non-standard providers into its models.json
            # instead.  Use the raw provider id so pi looks it up there.
            result.setdefault("provider", self.provider.id)
            return result
        if self.harness == "opencode":
            # Custom providers are declared under their own squid provider id
            # in OPENCODE_CONFIG_CONTENT (see execution_env) — the -m flag's
            # "provider/model" prefix must match that id, not the detected
            # openai/anthropic family.
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
