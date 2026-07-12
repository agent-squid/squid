"""Harnesses — Squid's coded coding-agent CLI integrations.

A harness owns command construction, protocol support, stream parsing, tool
normalization, session resume, and token semantics. The supported harness set
is fixed in code, but defaults such as protocol and provider are configurable
in ``squid.yaml``. What used to be called a
"driver" (ADR-0024) is renamed to harness here; the Claude Code harness is
`claudecode`, not `claude`, so it doesn't collide with the `claude`/
`anthropic` provider (see ADR-0027's collision report, superseded by that
rename).
"""

from __future__ import annotations

import re
from typing import Any

from .config import CLAUDE_PATH, CODEX_PATH, CURSOR_PATH, OPENCODE_PATH, PI_PATH, _cfg

SUPPORTED_HARNESSES = frozenset({"claudecode", "codex", "cursor", "opencode", "pi"})
SUPPORTED_PROTOCOLS = frozenset({"oneshot-cli", "interactive-cli", "interactive-pty"})
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# Only claudecode has a true persistent structured stdin/stdout protocol today.
SUPPORTED_PROTOCOLS_BY_HARNESS: dict[str, frozenset[str]] = {
    "claudecode": frozenset({"oneshot-cli", "interactive-cli"}),
    "codex": frozenset({"oneshot-cli"}),
    "cursor": frozenset({"oneshot-cli"}),
    "opencode": frozenset({"oneshot-cli"}),
    "pi": frozenset({"oneshot-cli"}),
}
_DEFAULT_PROTOCOL_BY_HARNESS: dict[str, str] = {
    "claudecode": "interactive-cli",
    "codex": "oneshot-cli",
    "cursor": "oneshot-cli",
    "opencode": "oneshot-cli",
    "pi": "oneshot-cli",
}

DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS = 3600

_HARNESS_PATHS: dict[str, "str | None"] = {
    "claudecode": CLAUDE_PATH,
    "codex": CODEX_PATH,
    "cursor": CURSOR_PATH,
    "opencode": OPENCODE_PATH,
    "pi": PI_PATH,
}

_DEFAULT_HARNESS_LABELS: dict[str, str] = {
    "claudecode": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "pi": "Pi",
}

# Install commands, matching README.md's table — the single source of truth
# for these strings; ADR-0028 deliberately doesn't duplicate them elsewhere.
_HARNESS_INSTALL: dict[str, str] = {
    "claudecode": "curl -fsSL https://claude.ai/install.sh | bash",
    "codex": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    "cursor": "curl -fsS https://cursor.com/install | bash",
    "opencode": "curl -fsSL https://opencode.ai/install | bash",
    "pi": "curl -fsSL https://pi.dev/install.sh | sh",
}

_DEFAULT_PROVIDER_BY_HARNESS: dict[str, str] = {
    "claudecode": "anthropic",
    "codex": "openai",
    "cursor": "cursor",
    "opencode": "nvidia",
    "pi": "nvidia",
}

_DEFAULT_SUPPORTED_APIS_BY_HARNESS: dict[str, frozenset[str]] = {
    "claudecode": frozenset({"/v1/messages"}),
    "codex": frozenset({"/v1/responses"}),
    "cursor": frozenset({"/native/cursor"}),
    "opencode": frozenset({"/v1/chat/completions", "/native/opencode"}),
    "pi": frozenset({"/v1/chat/completions"}),
}


def _validate_provider_id(field: str, value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{field} must be a provider id")
    return value


def _validate_supported_apis(field: str, value: Any) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    apis: list[str] = []
    for api in value:
        if not isinstance(api, str) or not api.strip():
            raise ValueError(f"{field} must contain non-empty strings")
        apis.append(api.strip())
    return frozenset(apis)


def _validate_protocol(harness_id: str, raw: Any) -> tuple[str, "float | None"]:
    timeout: float | None = None
    if isinstance(raw, str):
        protocol = raw
    elif isinstance(raw, dict):
        unknown = sorted(set(raw) - {"type", "timeout", "idle_timeout_seconds"})
        if unknown:
            raise ValueError(f"Harness {harness_id!r} protocol has unsupported field(s): {', '.join(unknown)}")
        protocol = raw.get("type")
        if "timeout" in raw:
            timeout_cfg = raw["timeout"]
            if not isinstance(timeout_cfg, dict):
                raise ValueError(f"Harness {harness_id!r} protocol.timeout must be a mapping")
            unknown_timeout = sorted(set(timeout_cfg) - {"idle_seconds", "idle_timeout_seconds"})
            if unknown_timeout:
                raise ValueError(
                    f"Harness {harness_id!r} protocol.timeout has unsupported field(s): "
                    + ", ".join(unknown_timeout)
                )
            if "idle_seconds" in timeout_cfg and "idle_timeout_seconds" in timeout_cfg:
                raise ValueError(
                    f"Harness {harness_id!r} protocol.timeout must not set both "
                    "idle_seconds and idle_timeout_seconds"
                )
            raw_timeout = timeout_cfg.get("idle_seconds", timeout_cfg.get("idle_timeout_seconds"))
            if raw_timeout is not None:
                if not isinstance(raw_timeout, (int, float)) or isinstance(raw_timeout, bool) or raw_timeout < 0:
                    raise ValueError(f"Harness {harness_id!r} protocol.timeout.idle_seconds must be non-negative")
                timeout = float(raw_timeout)
        if "idle_timeout_seconds" in raw:
            if timeout is not None:
                raise ValueError(
                    f"Harness {harness_id!r} must not set both protocol.timeout "
                    "and protocol.idle_timeout_seconds"
                )
            raw_timeout = raw["idle_timeout_seconds"]
            if not isinstance(raw_timeout, (int, float)) or isinstance(raw_timeout, bool) or raw_timeout < 0:
                raise ValueError(f"Harness {harness_id!r} protocol.idle_timeout_seconds must be non-negative")
            timeout = float(raw_timeout)
    else:
        raise ValueError(f"Harness {harness_id!r} protocol must be a string or mapping")

    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"Harness {harness_id!r} has unsupported protocol {protocol!r}")
    if protocol not in SUPPORTED_PROTOCOLS_BY_HARNESS[harness_id]:
        raise ValueError(f"Harness {harness_id!r} does not support protocol {protocol!r}")
    if timeout is not None and not str(protocol).startswith("interactive-"):
        raise ValueError(f"Harness {harness_id!r} protocol.idle_timeout_seconds requires an interactive protocol")
    return str(protocol), timeout


def _validate_harness(harness_id: str, raw: Any) -> dict[str, Any]:
    if harness_id not in SUPPORTED_HARNESSES:
        raise ValueError(f"Unknown harness {harness_id!r}")
    if not isinstance(raw, dict):
        raise ValueError(f"Harness {harness_id!r} must be a mapping")

    allowed = {"label", "protocol", "interactive", "default_provider", "supported_apis", "compatible_providers"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"Harness {harness_id!r} has unsupported field(s): {', '.join(unknown)}")

    result: dict[str, Any] = {}
    if "label" in raw:
        label = raw["label"]
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"Harness {harness_id!r} label must be a non-empty string")
        result["label"] = label

    if "protocol" in raw:
        protocol, timeout = _validate_protocol(harness_id, raw["protocol"])
        result["protocol"] = protocol
        if timeout is not None:
            result["idle_timeout_seconds"] = timeout

    if "interactive" in raw:
        if isinstance(raw.get("protocol"), dict) and "idle_timeout_seconds" in raw["protocol"]:
            raise ValueError(
                f"Harness {harness_id!r} must not set both protocol.idle_timeout_seconds "
                "and interactive.idle_timeout_seconds"
            )
        interactive = raw["interactive"]
        if not isinstance(interactive, dict):
            raise ValueError(f"Harness {harness_id!r} interactive must be a mapping")
        unknown_interactive = sorted(set(interactive) - {"idle_timeout_seconds"})
        if unknown_interactive:
            raise ValueError(
                f"Harness {harness_id!r} interactive has unsupported field(s): "
                + ", ".join(unknown_interactive)
            )
        if "idle_timeout_seconds" in interactive:
            timeout = interactive["idle_timeout_seconds"]
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
                raise ValueError(f"Harness {harness_id!r} interactive.idle_timeout_seconds must be non-negative")
            result["idle_timeout_seconds"] = timeout

    if "default_provider" in raw:
        result["default_provider"] = _validate_provider_id(
            f"Harness {harness_id!r} default_provider",
            raw["default_provider"],
        )

    if "compatible_providers" in raw:
        providers = raw["compatible_providers"]
        if not isinstance(providers, list) or not providers:
            raise ValueError(f"Harness {harness_id!r} compatible_providers must be a non-empty list")
        result["_legacy_compatible_providers"] = frozenset(
            _validate_provider_id(f"Harness {harness_id!r} compatible_providers", provider)
            for provider in providers
        )

    if "supported_apis" in raw:
        result["supported_apis"] = _validate_supported_apis(
            f"Harness {harness_id!r} supported_apis",
            raw["supported_apis"],
        )

    return result


def _validate_harness_config(configured: Any) -> dict[str, dict[str, Any]]:
    if configured is None:
        return {}
    if not isinstance(configured, dict):
        raise ValueError("harnesses must be a mapping")
    return {harness_id: _validate_harness(harness_id, raw) for harness_id, raw in configured.items()}


def _configured_harnesses() -> dict[str, dict[str, Any]]:
    configured = _validate_harness_config(_cfg.get("harnesses"))
    result: dict[str, dict[str, Any]] = {}
    for harness_id in sorted(SUPPORTED_HARNESSES):
        result[harness_id] = {
            "label": _DEFAULT_HARNESS_LABELS[harness_id],
            "protocol": _DEFAULT_PROTOCOL_BY_HARNESS[harness_id],
            "default_provider": _DEFAULT_PROVIDER_BY_HARNESS[harness_id],
            "supported_apis": _DEFAULT_SUPPORTED_APIS_BY_HARNESS[harness_id],
            "idle_timeout_seconds": DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS,
        }
        result[harness_id].update(configured.get(harness_id, {}))
    return result


_HARNESS_CONFIG = _configured_harnesses()
_HARNESS_LABELS: dict[str, str] = {
    harness_id: str(config["label"]) for harness_id, config in _HARNESS_CONFIG.items()
}
DEFAULT_PROTOCOL_BY_HARNESS: dict[str, str] = {
    harness_id: str(config["protocol"]) for harness_id, config in _HARNESS_CONFIG.items()
}
HARNESS_DEFAULT_PROVIDER: dict[str, str] = {
    harness_id: str(config["default_provider"]) for harness_id, config in _HARNESS_CONFIG.items()
}
HARNESS_SUPPORTED_APIS: dict[str, frozenset[str]] = {
    harness_id: config["supported_apis"] for harness_id, config in _HARNESS_CONFIG.items()
}


def protocol_for(harness_id: str, requested: "str | None") -> str:
    protocol = requested or DEFAULT_PROTOCOL_BY_HARNESS[harness_id]
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"Harness {harness_id!r} has unsupported protocol {protocol!r}")
    if protocol not in SUPPORTED_PROTOCOLS_BY_HARNESS[harness_id]:
        raise ValueError(f"Harness {harness_id!r} does not support protocol {protocol!r}")
    return protocol


def require_harness(harness_id: str) -> str:
    if harness_id not in SUPPORTED_HARNESSES:
        raise ValueError(f"Unknown harness {harness_id!r}")
    return harness_id


def harness_path(harness_id: str) -> "str | None":
    return _HARNESS_PATHS.get(harness_id)


def is_installed(harness_id: str) -> bool:
    return bool(_HARNESS_PATHS.get(harness_id))


def default_provider_for(harness_id: str) -> str:
    return HARNESS_DEFAULT_PROVIDER[harness_id]


def interactive_idle_timeout_seconds(harness_id: str) -> float:
    return float(_HARNESS_CONFIG[harness_id]["idle_timeout_seconds"])


def supported_apis_for(harness_id: str) -> frozenset[str]:
    return HARNESS_SUPPORTED_APIS[harness_id]


def _compatible_providers_for(harness_id: str) -> list[str]:
    from .providers import PROVIDERS

    harness_apis = supported_apis_for(harness_id)
    provider_ids = {
        provider_id
        for provider_id, provider in PROVIDERS.items()
        if harness_apis & provider.supported_apis
    }
    provider_ids.update(_HARNESS_CONFIG[harness_id].get("_legacy_compatible_providers", frozenset()))
    default_provider = HARNESS_DEFAULT_PROVIDER[harness_id]
    if default_provider in PROVIDERS:
        provider_ids.add(default_provider)
    return sorted(provider_ids)


def list_harnesses() -> list[dict[str, Any]]:
    """For GET /config/harnesses — settings UI install checklist."""
    return [
        {
            "id": harness_id,
            "label": _HARNESS_LABELS[harness_id],
            "install_cmd": _HARNESS_INSTALL[harness_id],
            "installed": is_installed(harness_id),
            "protocol": DEFAULT_PROTOCOL_BY_HARNESS[harness_id],
            "interactive": {"idle_timeout_seconds": interactive_idle_timeout_seconds(harness_id)},
            "default_provider": HARNESS_DEFAULT_PROVIDER[harness_id],
            "supported_apis": sorted(HARNESS_SUPPORTED_APIS[harness_id]),
            "compatible_providers": _compatible_providers_for(harness_id),
        }
        for harness_id in sorted(SUPPORTED_HARNESSES)
    ]
