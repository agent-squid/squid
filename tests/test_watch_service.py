import pytest

from agent import stats_db, watches
from agent.watch_config import load_watch_configuration
from agent.watch_policy import OPERATIONS
from agent.watch_service import WatchControl, WatchDenied, WatchService


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    stats_db.init_db()
    now = [100.0]
    config = load_watch_configuration({'watches': {'enabled': True, 'registry': {
        'probes': {'local': {'argv': ['/missing']}}}}})
    service = WatchService(config, clock=lambda: now[0])
    control = WatchControl(service)
    return service, control, now


def mint(control, *, topic='squid', operations=OPERATIONS, grants=(), turn='turn'):
    return control.mint(turn=turn, topic=topic, operations=operations,
                        grants=grants, expires_at=1000)


def create(service, token, **kwargs):
    return service.create(token, **(dict(adapter='local', target='task:1',
        request_key='request', expires_at=2000) | kwargs))


def authorize(service, control, token, row):
    grant = control.record_grant(topic=row['topic'], watch_id=row['id'],
                                 user_action='authenticated-action:1', expires_at=row['expires_at'])
    granted = mint(control, grants=[grant])
    return grant, granted


def test_proposal_idempotence_topic_isolation_and_public_fields(setup):
    service, control, _ = setup
    token = mint(control)
    row = create(service, token)
    assert row['status'] == 'proposed'
    assert create(service, token) == row
    assert 'claim_token' not in row and 'lease_until' not in row
    assert service.show(token, row['id']) == row
    assert service.list(token) == [row]
    other = mint(control, topic='other')
    assert service.list(other) == []
    with pytest.raises(WatchDenied):
        service.show(other, row['id'])
    with pytest.raises(ValueError, match='not found'):
        service.transition(other, row['id'], version=0, action='cancel')
    with pytest.raises(ValueError, match='different'):
        create(service, token, target='other')


def test_activation_requires_exact_durable_grant_and_token_ceiling(setup):
    service, control, _ = setup
    token = mint(control)
    row = create(service, token)
    with pytest.raises(WatchDenied):
        service.transition(token, row['id'], version=0, action='activate', grant_id='I am the user')
    grant, granted = authorize(service, control, token, row)
    with pytest.raises(WatchDenied):
        service.transition(token, row['id'], version=0, action='activate', grant_id=grant)
    active = service.transition(granted, row['id'], version=0, action='activate', grant_id=grant)
    assert active['status'] == 'active'
    other = create(service, token, request_key='second')
    with pytest.raises(WatchDenied):
        service.transition(granted, other['id'], version=0, action='activate', grant_id=grant)


def test_revocation_pauses_and_invalidates_claim_and_keyed_replay(setup):
    service, control, now = setup
    token = mint(control)
    row = create(service, token)
    grant, granted = authorize(service, control, token, row)
    service.transition(granted, row['id'], version=0, action='activate', grant_id=grant, operation_key='activate')
    claim = watches.claim_due(now=100, lease_seconds=60, limit=1)[0]
    control.revoke_grant(topic='squid', grant_id=grant)
    paused = service.show(token, row['id'])
    assert paused['status'] == 'paused' and paused['pause_reason'] == 'grant_revoked'
    assert not watches.finish_poll(claim, now=101, observation=watches.Observation('late'))
    for version, action, key in [(0, 'activate', 'activate'), (paused['version'], 'resume', None)]:
        with pytest.raises(WatchDenied):
            service.transition(granted, row['id'], version=version, action=action,
                               grant_id=grant, operation_key=key)
    control.revoke_grant(topic='squid', grant_id=grant)  # idempotent


def test_tokens_expire_revoke_and_do_not_survive_service_restart(setup):
    service, control, now = setup
    token = mint(control)
    assert token not in repr(service._credentials)
    assert service.list(token) == []
    restarted = WatchService(service.configuration, clock=lambda: now[0])
    with pytest.raises(WatchDenied):
        restarted.list(token)
    control.revoke_turn('turn')
    with pytest.raises(WatchDenied):
        service.list(token)
    token = mint(control)
    now[0] = 1000
    with pytest.raises(WatchDenied):
        service.list(token)


def test_operation_scope_and_unsupported_writes(setup):
    service, control, _ = setup
    token = mint(control, operations=['list'])
    assert service.list(token) == []
    with pytest.raises(WatchDenied):
        create(service, token)
    with pytest.raises(ValueError):
        mint(control, operations=['publish'])
    with pytest.raises(ValueError):
        control.mint(turn='t', topic='squid', operations=['list'], expires_at=3701)
    with pytest.raises(ValueError):
        service.transition(token, 'watch', version=0, action='publish')


def test_grants_persist_but_new_turn_needs_fresh_credential(setup):
    service, control, now = setup
    token = mint(control)
    row = create(service, token)
    grant, _ = authorize(service, control, token, row)
    stats_db.init_db()
    restarted = WatchService(service.configuration, clock=lambda: now[0])
    fresh = mint(WatchControl(restarted), grants=[grant])
    assert restarted.transition(fresh, row['id'], version=0, action='activate', grant_id=grant)['status'] == 'active'


def test_grant_cannot_extend_or_cross_topic(setup):
    service, control, _ = setup
    row = create(service, mint(control))
    for topic, expiry in [('other', 2000), ('squid', 1999), ('squid', 100000)]:
        with pytest.raises(WatchDenied):
            control.record_grant(topic=topic, watch_id=row['id'], user_action='trusted:1', expires_at=expiry)


def test_expired_grant_cannot_activate(setup):
    service, control, now = setup
    row = create(service, mint(control), expires_at=200)
    grant, token = authorize(service, control, None, row)
    now[0] = 200
    with pytest.raises(WatchDenied):
        service.transition(token, row['id'], version=0, action='activate', grant_id=grant)


def test_default_expiry_retry_preserves_window(setup):
    service, control, now = setup
    token = mint(control)
    row = service.create(token, adapter='local', target='task:1', request_key='default')
    now[0] += 100
    assert service.create(token, adapter='local', target='task:1', request_key='default')['expires_at'] == row['expires_at']


def test_shutdown_revokes_and_disables_control(setup):
    service, control, _ = setup
    token = mint(control)
    row = create(service, token)
    service.close()
    with pytest.raises(WatchDenied):
        service.list(token)
    with pytest.raises(WatchDenied):
        mint(control)
    with pytest.raises(WatchDenied):
        control.record_grant(topic='squid', watch_id=row['id'], user_action='trusted', expires_at=2000)


def test_policy_bounded_creation_and_default_expiry(setup):
    old_service, _, now = setup
    config = load_watch_configuration({'watches': {'enabled': True,
        'registry': {'probes': {'local': {'argv': ['/missing']}}},
        'policy': {'min_poll_seconds': 60, 'max_lifetime_seconds': 300}}})
    service = WatchService(config, clock=lambda: now[0])
    control = WatchControl(service)
    token = mint(control)
    with pytest.raises(ValueError, match='minimum'):
        create(service, token)
    with pytest.raises(ValueError, match='expiry'):
        create(service, token, poll_interval=60)
    with pytest.raises(WatchDenied):
        create(service, token, adapter='unknown', poll_interval=60)
    row = service.create(token, adapter='local', target='task', request_key='r', poll_interval=60)
    assert row['expires_at'] == 400
    now[0] = 450
    retry = service.create(token, adapter='local', target='task', request_key='r', poll_interval=60)
    assert retry['expires_at'] == 400
    with pytest.raises(WatchDenied):
        old_service.list(token)


def test_topic_disabled_and_operations_cannot_be_expanded(setup):
    _, _, now = setup
    config = load_watch_configuration({'watches': {'enabled': True,
        'policy': {'operations': ['list']},
        'topics': {'disabled': {'enabled': False}, 'squid': {'operations': ['create', 'list']}}}})
    service = WatchService(config, clock=lambda: now[0])
    control = WatchControl(service)
    assert service.list(mint(control, operations=['list'])) == []
    with pytest.raises(WatchDenied):
        mint(control, operations=['create'])
    with pytest.raises(WatchDenied):
        mint(control, topic='disabled', operations=['list'])


def test_explicit_expiry_retry_after_expiry_is_idempotent(setup):
    service, control, now = setup
    token = mint(control)
    row = create(service, token, expires_at=200)
    now[0] = 250
    assert create(service, token, expires_at=200) == row


@pytest.mark.parametrize('limit,offset', [(0, 0), (101, 0), (True, 0), (1, -1), (1, False)])
def test_pagination_bounds(setup, limit, offset):
    service, control, _ = setup
    with pytest.raises(ValueError):
        service.list(mint(control), limit=limit, offset=offset)


def test_revoke_only_pauses_after_last_full_lifetime_grant(setup):
    service, control, now = setup
    token = mint(control)
    row = create(service, token)
    first, granted = authorize(service, control, token, row)
    second, _ = authorize(service, control, token, row)
    service.transition(granted, row['id'], version=0, action='activate', grant_id=first)
    claim = watches.claim_due(now=100, lease_seconds=60, limit=1)[0]
    control.revoke_grant(topic='squid', grant_id=first)
    active = service.show(token, row['id'])
    assert active['status'] == 'active' and active['version'] == 1
    assert watches.finish_poll(claim, now=101, observation=watches.Observation('still-covered'))
    control.revoke_grant(topic='squid', grant_id=second)
    paused = service.show(token, row['id'])
    assert paused['status'] == 'paused' and paused['pause_reason'] == 'grant_revoked'
    assert paused['version'] == 2


def test_grant_foreign_key_cascade_is_enforced(setup):
    service, control, _ = setup
    token = mint(control)
    row = create(service, token)
    grant, _ = authorize(service, control, token, row)
    conn = watches._connect()
    try:
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        with conn:
            conn.execute('DELETE FROM watches WHERE id=?', (row['id'],))
        assert conn.execute('SELECT 1 FROM watch_grants WHERE id=?', (grant,)).fetchone() is None
    finally:
        conn.close()
