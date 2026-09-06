"""The Shore capability registry: a fail-closed allowlist of exactly which
ADR-0040 message types, protocol versions, payload shapes, and scopes a
remote browser may reach through a Shore connection.

Normative source: docs/shore-protocol-v1.md, "Initial capability registry".
This module only encodes the registry as data and validates one decrypted
ADR-0040 frame against it; it does not dispatch into the shared realtime core
(agent/server.py's `_realtime_catchup`/`_realtime_snapshot`) or hold any
per-device session state — that is Milestone 4.3/4.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .shore_crypto import ShoreProtocolError

# The full set of ADR-0040 message types a browser may send, hand-mirrored
# from realtime_v1's dispatch (agent/server.py, the "subscribe"/"unsubscribe"/
# .../"worktree.auto_resolve" branches) -- there is no shared source between
# the two yet, so a type added there must be added here too, or it silently
# misclassifies as shore_unsupported_type instead of shore_capability_denied.
# 4.8's negative-test suite is where this drift should get caught for real.
# Used only to distinguish "not a real ADR-0040 type" (shore_unsupported_type)
# from "a real type this device isn't granted" (shore_capability_denied) --
# both fail closed either way.
ADR0040_BROWSER_TO_HOST_TYPES = frozenset({
    "subscribe", "unsubscribe", "ack", "ping", "pong",
    "chat.start", "chat.cancel",
    "auth.start", "auth.input", "auth.resize", "auth.cancel",
    "worktree.auto_resolve",
})

# The plaintext ADR-0040 frame's own closed top-level schema, independent of
# the outer envelope's fields (account_id/device_id/seq/... are the envelope's
# concern, already validated by open_envelope before this module runs).
_FRAME_FIELDS = frozenset({"v", "type", "payload"})


def _valid_scopes_shape(value: Any) -> bool:
    return isinstance(value, list)


def _valid_cursor(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_event_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _authorize_dashboard_read_scope(requested: Any) -> Optional[list[dict]]:
    """`dashboard.read.v1` authorizes only the global lifecycle feed.

    Stricter than `_authorize_realtime_scopes` (agent/server.py), which also
    accepts topic/agent-scoped requests for the fully trusted local session --
    see docs/plans/adr-0039-shore-remote-access.md Milestone 4 open question 2.
    Confirm that reading before this is relied on for anything beyond the
    global feed.
    """
    if not isinstance(requested, list) or not requested:
        return None
    if any(scope != {"lifecycle": "global"} for scope in requested):
        return None
    return [{"lifecycle": "global"}]


@dataclass(frozen=True)
class PayloadField:
    required: bool
    valid: Callable[[Any], bool]


@dataclass(frozen=True)
class ShoreCapability:
    name: str
    versions: frozenset[int]
    types: frozenset[str]
    # type -> {field_name: PayloadField}; a closed schema per type, so a field
    # absent here is rejected even if the direct /ws/v1 path tolerates it.
    payload_schemas: Mapping[str, Mapping[str, PayloadField]]
    # Consulted for whichever type's schema declares a "scopes" field; None if
    # the capability has no scoped type. A scoped type with no authorizer
    # fails closed (shore_capability_denied) rather than skipping the check --
    # see authorize_capability_frame.
    authorize_scope: Optional[Callable[[Any], Optional[list[dict]]]] = None

    def __post_init__(self) -> None:
        # A type declared in `types` but missing from `payload_schemas` (or
        # vice versa) would raise a bare KeyError deep in
        # authorize_capability_frame instead of this class's own
        # ShoreProtocolError contract; catch the mismatch at registry
        # construction time instead of at frame-dispatch time.
        if set(self.types) != set(self.payload_schemas):
            raise ValueError(f"{self.name}: types and payload_schemas must declare the same message types")


DASHBOARD_READ_V1 = ShoreCapability(
    name="dashboard.read.v1",
    versions=frozenset({1}),
    types=frozenset({"subscribe", "unsubscribe", "ack", "ping", "pong"}),
    payload_schemas={
        "subscribe": {
            "scopes": PayloadField(False, _valid_scopes_shape),
            "cursor": PayloadField(False, _valid_cursor),
        },
        "unsubscribe": {},
        "ack": {"event_id": PayloadField(True, _valid_event_id)},
        "ping": {},
        "pong": {},
    },
    authorize_scope=_authorize_dashboard_read_scope,
)

SHORE_CAPABILITIES: Mapping[str, ShoreCapability] = {"dashboard.read.v1": DASHBOARD_READ_V1}

# Devices pair into this capability set until a later, separately reviewed
# grant flow (e.g. shell.exec.v1) adds to it -- additive, never a retrofit.
DEFAULT_CAPABILITIES: tuple[str, ...] = ("dashboard.read.v1",)

SUPPORTED_VERSIONS = frozenset().union(*(capability.versions for capability in SHORE_CAPABILITIES.values()))


def authorize_capability_frame(capability_names: Any, frame: Any) -> dict[str, Any]:
    """Enforce the registry's fail-closed dispatch order on one decrypted
    ADR-0040 frame.

    Order: closed frame schema (unknown top-level fields, unsupported `v`,
    before inspecting `type`) -> known ADR-0040 type -> granted capability ->
    strict per-type payload schema -> scope authorization. Returns `frame`
    with `payload.scopes` (only for a type whose schema declares it) replaced
    by the authorized, canonicalized scopes. Raises ShoreProtocolError with a
    stable pre-dispatch code (docs/shore-protocol-v1.md) on any failure,
    never revealing which check failed beyond that code.
    """
    version = frame.get("v") if isinstance(frame, dict) else None
    if (not isinstance(frame, dict) or set(frame) != _FRAME_FIELDS
            or isinstance(version, bool) or version not in SUPPORTED_VERSIONS):
        raise ShoreProtocolError("shore_invalid_frame")
    message_type = frame.get("type")
    payload = frame.get("payload")
    if not isinstance(message_type, str) or not isinstance(payload, dict):
        raise ShoreProtocolError("shore_invalid_frame")
    if message_type not in ADR0040_BROWSER_TO_HOST_TYPES:
        raise ShoreProtocolError("shore_unsupported_type")

    names = capability_names if isinstance(capability_names, (list, tuple, frozenset, set)) else ()
    capability = next(
        (SHORE_CAPABILITIES[name] for name in names
         if name in SHORE_CAPABILITIES and message_type in SHORE_CAPABILITIES[name].types),
        None,
    )
    if capability is None:
        raise ShoreProtocolError("shore_capability_denied")

    schema = capability.payload_schemas[message_type]
    if set(payload) - set(schema):
        raise ShoreProtocolError("shore_invalid_frame")
    for field_name, field in schema.items():
        if field_name not in payload:
            if field.required:
                raise ShoreProtocolError("shore_invalid_frame")
            continue
        if not field.valid(payload[field_name]):
            raise ShoreProtocolError("shore_invalid_frame")

    if "scopes" in schema:
        # Driven by the schema, not a hardcoded "subscribe" check, so a
        # future scoped type is authorized the same way automatically. A
        # scoped type with no authorizer wired up fails closed instead of
        # silently passing whatever scopes the device asked for through
        # unauthorized.
        if capability.authorize_scope is None:
            raise ShoreProtocolError("shore_capability_denied")
        authorized = capability.authorize_scope(payload.get("scopes", []))
        if authorized is None:
            raise ShoreProtocolError("shore_unauthorized_scope")
        frame = {**frame, "payload": {**payload, "scopes": authorized}}
    return frame
