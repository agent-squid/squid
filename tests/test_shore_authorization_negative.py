"""Milestone 4.8: authorization/negative test suite.

`tests/test_shore_capabilities.py` already exhaustively covers
`authorize_capability_frame`'s own fail-closed contract: every real ADR-0040
type outside `dashboard.read.v1` (chat.start/chat.cancel/auth.*/
worktree.auto_resolve), every non-global scope shape, unsupported/missing
protocol version rejected before capability lookup, and extra/unknown
fields at both the frame and payload level. This file covers what that
unit-level module can't see on its own:

1. A revoked or wrong-epoch device fails at `ShoreChannel`'s identity check
   *before* `authorize_capability_frame` ever runs, even for an otherwise
   fully-granted command -- proving check ordering, not just that each check
   works in isolation.
2. `ShoreProtocolError`'s stable code vocabulary can't leak per-call dynamic
   detail, checked exhaustively (every raise site in the Shore host stack via
   static analysis) rather than by a handful of example assertions that could
   miss a future call site.

The plan's remaining 4.8 bullet -- "denied frames still count against the
existing per-socket frame-rate limit in shore/src/index.ts" -- has no
separate test here: the relay increments `meta.rateCount` for every
non-empty binary frame (`webSocketMessage`, shore's `src/index.ts`) before
any role- or content-based branching, since relayed content is E2E
ciphertext the relay can't decrypt. There is no code path where a frame's
eventual host-side authorization outcome could exempt it from that counter --
that outcome doesn't exist yet at relay time -- so the existing
`test/shore.test.ts` "rate_limited" coverage of that same unconditional
counter already establishes this; content-specific denial can't behave
differently at a layer that never inspects content.

See docs/plans/adr-0039-shore-remote-access.md Milestone 4.8.
"""

import ast
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from agent.shore_capabilities import authorize_capability_frame
from agent.shore_crypto import ShoreProtocolError, canonical
from agent.shore_transport import ShoreChannel

from tests.test_shore_transport import ACCOUNT, HOST, DEVICE, NOW, browser_frame, pair

SHORE_HOST_MODULES = ["agent/shore_capabilities.py", "agent/shore_transport.py", "agent/shore_crypto.py"]


@pytest.mark.asyncio
async def test_revoked_device_fails_identity_check_before_capability_check(tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)
    await pair(channel, browser_signing, browser_agreement)
    assert channel.revoke_device(DEVICE) is True

    # "subscribe" with a global scope is a fully-granted dashboard.read.v1
    # command -- if the identity check were skipped, or ordered after
    # capability authorization, this would succeed. It must instead fail
    # exactly like a never-paired device would.
    request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                             "subscribe", {"scopes": [{"lifecycle": "global"}]})
    with pytest.raises(ShoreProtocolError, match="shore_untrusted_device"):
        await channel.handle(canonical(request), now_ms=NOW)


@pytest.mark.asyncio
async def test_wrong_key_epoch_device_fails_identity_check_before_capability_check(tmp_path):
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    browser_signing, browser_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    original = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement, key_epoch=1)
    await pair(original, browser_signing, browser_agreement)

    # Same on-disk trust store (same state_dir), same paired device, but a
    # channel instance running under a rotated key epoch -- as after a real
    # host-key rotation. browser_frame() seals at key_epoch=1 to match the
    # device's actual paired epoch; the *channel's* epoch (2) is what's
    # mismatched, so this isolates the epoch check from a signature failure.
    rotated = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement, key_epoch=2)
    request = browser_frame(browser_signing, browser_agreement, host_agreement.public_key(), 1,
                             "subscribe", {"scopes": [{"lifecycle": "global"}]})
    with pytest.raises(ShoreProtocolError, match="shore_untrusted_device"):
        await rotated.handle(canonical(request), now_ms=NOW)


def _shore_protocol_error_call_sites(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text())
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "ShoreProtocolError"
    ]


def _is_closed_string_literal(node: ast.AST) -> bool:
    """A bare string constant, or a ternary between two (recursively) closed
    string literals -- e.g. `"a" if cond else "b"`, which several call sites
    use to pick between two fixed stable codes based on *which* known,
    hardcoded field failed (agent/shore_crypto.py's identity check), not on
    any per-request value. Still closed: every reachable leaf is a literal,
    so nothing from the request or the field's actual value can appear in the
    code string. An f-string, `.format()` call, `%` message, or concatenation
    with a variable is rejected either way, since none of those are `Constant`
    or `IfExp` nodes."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.IfExp):
        return _is_closed_string_literal(node.body) and _is_closed_string_literal(node.orelse)
    return False


@pytest.mark.parametrize("relative_path", SHORE_HOST_MODULES)
def test_shore_protocol_error_call_sites_never_pass_dynamic_detail(relative_path):
    """Every `raise ShoreProtocolError(...)` in the Shore host stack must pass
    exactly one closed string-literal argument (see `_is_closed_string_literal`)
    -- never an f-string, a `.format()`/`%` message, or a second argument built
    from request content. `ShoreProtocolError.__init__` only accepts a bare
    `code` (see agent/shore_crypto.py), so this is a real structural
    guarantee, not a convention that could quietly rot: a future call site
    that embeds per-request detail (a device_id, a bad field name, a raw
    exception message) into the code would leak more than the protocol doc's
    stable vocabulary allows, and this test fails on it immediately rather
    than waiting for enumerated example cases to happen to cover it.
    """
    module_path = Path(__file__).resolve().parent.parent / relative_path
    sites = _shore_protocol_error_call_sites(module_path)
    assert sites, f"expected at least one ShoreProtocolError call site in {relative_path}"
    for node in sites:
        assert not node.keywords, f"{relative_path}:{node.lineno} passes a keyword argument"
        assert len(node.args) == 1, f"{relative_path}:{node.lineno} must pass exactly one argument"
        arg = node.args[0]
        assert _is_closed_string_literal(arg), (
            f"{relative_path}:{node.lineno} does not pass a closed string literal (found {ast.dump(arg)})"
        )


@pytest.mark.asyncio
async def test_identity_layer_and_capability_layer_denials_are_equally_opaque(tmp_path):
    """Concrete instance of the static guarantee above: an untrusted-signer
    failure (identity layer, agent/shore_transport.py/shore_crypto.py) and an
    unknown-top-level-field failure (capability layer,
    agent/shore_capabilities.py) both surface as nothing more than their bare
    stable code -- proving a peer can't tell which layer, or which specific
    check within it, actually failed from the error content alone.
    """
    host_signing, host_agreement = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    channel = ShoreChannel(tmp_path, account_id=ACCOUNT, host_id=HOST,
        host_signing=host_signing, host_agreement=host_agreement)

    untrusted_signing = ed25519.Ed25519PrivateKey.generate()
    untrusted_agreement = x25519.X25519PrivateKey.generate()
    injected = browser_frame(untrusted_signing, untrusted_agreement, host_agreement.public_key(), 1,
                              "subscribe", {"scopes": [{"lifecycle": "global"}]})
    with pytest.raises(ShoreProtocolError) as identity_failure:
        await channel.handle(canonical(injected), now_ms=NOW)

    with pytest.raises(ShoreProtocolError) as capability_failure:
        authorize_capability_frame(["dashboard.read.v1"], {
            "v": 1, "type": "ping", "payload": {}, "unexpected_top_level_field": True,
        })

    assert str(identity_failure.value) == identity_failure.value.code == "shore_untrusted_device"
    assert str(capability_failure.value) == capability_failure.value.code == "shore_invalid_frame"
