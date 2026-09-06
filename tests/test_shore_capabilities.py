import pytest

from agent.shore_capabilities import (
    DEFAULT_CAPABILITIES, PayloadField, SHORE_CAPABILITIES, ShoreCapability,
    authorize_capability_frame,
)
from agent.shore_crypto import ShoreProtocolError

CAPS = list(DEFAULT_CAPABILITIES)


def test_default_capabilities_is_dashboard_read_only():
    assert DEFAULT_CAPABILITIES == ("dashboard.read.v1",)
    assert set(DEFAULT_CAPABILITIES) <= set(SHORE_CAPABILITIES)


def test_capability_construction_rejects_types_payload_schemas_mismatch():
    # A type in `types` missing from `payload_schemas` (or vice versa) would
    # otherwise raise a bare KeyError deep inside authorize_capability_frame
    # instead of this module's own ShoreProtocolError contract.
    with pytest.raises(ValueError, match="types and payload_schemas"):
        ShoreCapability(
            name="broken.v1", versions=frozenset({1}),
            types=frozenset({"ping", "pong"}),
            payload_schemas={"ping": {}},
        )
    with pytest.raises(ValueError, match="types and payload_schemas"):
        ShoreCapability(
            name="broken.v1", versions=frozenset({1}),
            types=frozenset({"ping"}),
            payload_schemas={"ping": {}, "pong": {}},
        )


def test_a_scoped_type_with_no_authorizer_fails_closed(monkeypatch):
    # Fail-closed by construction: a future capability that lists a scoped
    # type (its schema declares "scopes") but forgets to wire authorize_scope
    # must deny every request of that type, not silently allow whatever
    # scopes the device asked for through unauthorized.
    misconfigured = ShoreCapability(
        name="misconfigured.v1", versions=frozenset({1}),
        types=frozenset({"subscribe"}),
        payload_schemas={"subscribe": {"scopes": PayloadField(False, lambda v: isinstance(v, list))}},
        authorize_scope=None,
    )
    import agent.shore_capabilities as shore_capabilities
    monkeypatch.setitem(shore_capabilities.SHORE_CAPABILITIES, "misconfigured.v1", misconfigured)
    frame = {"v": 1, "type": "subscribe", "payload": {"scopes": [{"lifecycle": "global"}]}}
    with pytest.raises(ShoreProtocolError, match="shore_capability_denied"):
        authorize_capability_frame(["misconfigured.v1"], frame)


def test_subscribe_with_global_scope_is_authorized_and_canonicalized():
    frame = {"v": 1, "type": "subscribe", "payload": {"scopes": [{"lifecycle": "global"}], "cursor": 5}}
    authorized = authorize_capability_frame(CAPS, frame)
    assert authorized["payload"]["scopes"] == [{"lifecycle": "global"}]
    assert authorized["payload"]["cursor"] == 5


@pytest.mark.parametrize("scopes", [
    [{"topic": "some-topic"}],
    [{"topic": "some-topic", "agent": "codex"}],
    [{"lifecycle": "global"}, {"topic": "some-topic"}],
    [],
    None,
])
def test_subscribe_rejects_non_global_scopes(scopes):
    frame = {"v": 1, "type": "subscribe", "payload": {"scopes": scopes} if scopes is not None else {}}
    with pytest.raises(ShoreProtocolError, match="shore_unauthorized_scope"):
        authorize_capability_frame(CAPS, frame)


@pytest.mark.parametrize("message_type", [
    "chat.start", "chat.cancel", "auth.start", "auth.input", "auth.resize",
    "auth.cancel", "worktree.auto_resolve",
])
def test_real_adr0040_types_outside_the_capability_are_denied(message_type):
    frame = {"v": 1, "type": message_type, "payload": {}}
    with pytest.raises(ShoreProtocolError, match="shore_capability_denied"):
        authorize_capability_frame(CAPS, frame)


@pytest.mark.parametrize("message_type", ["hello", "subscribed", "snapshot", "command.result", "not_a_real_type"])
def test_unknown_or_server_only_types_are_rejected_as_unsupported(message_type):
    frame = {"v": 1, "type": message_type, "payload": {}}
    with pytest.raises(ShoreProtocolError, match="shore_unsupported_type"):
        authorize_capability_frame(CAPS, frame)


@pytest.mark.parametrize("version", [0, 2, "1", None, True])
def test_unsupported_or_missing_version_is_rejected_before_capability_lookup(version):
    frame = {"v": version, "type": "ping", "payload": {}}
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        authorize_capability_frame(CAPS, frame)


@pytest.mark.parametrize("frame", [
    {"v": 1, "type": "ping", "payload": {}, "request_id": "extra"},
    {"v": 1, "type": "ping"},
    {"type": "ping", "payload": {}},
    "not a dict",
    {"v": 1, "type": 5, "payload": {}},
    {"v": 1, "type": "ping", "payload": []},
])
def test_malformed_top_level_frame_shape_is_rejected(frame):
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        authorize_capability_frame(CAPS, frame)


@pytest.mark.parametrize("payload", [
    {"scopes": [{"lifecycle": "global"}], "unexpected": True},
    {"scopes": "not-a-list"},
    {"cursor": -1},
    {"cursor": "5"},
])
def test_subscribe_payload_schema_is_strict(payload):
    frame = {"v": 1, "type": "subscribe", "payload": payload}
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame|shore_unauthorized_scope"):
        authorize_capability_frame(CAPS, frame)


def test_ack_requires_a_non_negative_event_id():
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        authorize_capability_frame(CAPS, {"v": 1, "type": "ack", "payload": {}})
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        authorize_capability_frame(CAPS, {"v": 1, "type": "ack", "payload": {"event_id": -1}})
    authorized = authorize_capability_frame(CAPS, {"v": 1, "type": "ack", "payload": {"event_id": 3}})
    assert authorized["payload"]["event_id"] == 3


@pytest.mark.parametrize("message_type", ["unsubscribe", "ping", "pong"])
def test_empty_payload_types_reject_any_extra_field(message_type):
    with pytest.raises(ShoreProtocolError, match="shore_invalid_frame"):
        authorize_capability_frame(CAPS, {"v": 1, "type": message_type, "payload": {"extra": 1}})
    authorized = authorize_capability_frame(CAPS, {"v": 1, "type": message_type, "payload": {}})
    assert authorized["payload"] == {}


def test_a_device_with_no_granted_capabilities_is_denied_every_type():
    with pytest.raises(ShoreProtocolError, match="shore_capability_denied"):
        authorize_capability_frame([], {"v": 1, "type": "ping", "payload": {}})
    with pytest.raises(ShoreProtocolError, match="shore_capability_denied"):
        authorize_capability_frame(["unknown.capability.v1"], {"v": 1, "type": "ping", "payload": {}})
