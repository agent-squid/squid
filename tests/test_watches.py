import asyncio
import sqlite3

import pytest

from agent import stats_db, watches


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'watch.db')
    stats_db.init_db()
    stats_db.init_db()  # additive and repeatable


def create(**overrides):
    return watches.create_watch(**(dict(topic='squid', adapter='fake', target='pr:1',
        request_key='request', poll_interval=30, expires_at=10000, now=100) | overrides))


def activate(row):
    return watches.transition_watch(row['id'], topic=row['topic'], version=row['version'],
                                    action='activate', now=100)


def rows(table):
    conn = stats_db._connect()
    try:
        return [dict(r) for r in conn.execute(f'SELECT * FROM {table}')]
    finally:
        conn.close()


def claim(now=100):
    return watches.claim_due(now=now, lease_seconds=60, limit=4)


def test_idempotent_proposal_and_versioned_lifecycle(db):
    row = create()
    assert create()['id'] == row['id']
    assert row['status'] == 'proposed' and claim() == []
    with pytest.raises(ValueError, match='different'):
        create(target='pr:2')
    active = activate(row)
    with pytest.raises(ValueError, match='stale'):
        watches.transition_watch(row['id'], topic='squid', version=0, action='pause', now=101)
    with pytest.raises(ValueError, match='not found'):
        watches.transition_watch(row['id'], topic='other', version=1, action='pause', now=101)
    assert active['version'] == 1


def test_restart_lease_recovery_rejects_old_owner(db):
    activate(create())
    old = claim()[0]
    assert claim() == []
    stats_db.init_db()
    new = claim(161)[0]
    observation = watches.Observation('cursor', (('failure:1', {'sha': 'head'}),))
    assert not watches.finish_poll(old, now=162, observation=observation)
    assert watches.finish_poll(new, now=162, observation=observation)
    assert len(rows('watch_events')) == 1
    assert rows('watches')[0]['cursor'] == 'cursor'


def test_ingestion_is_atomic_deduplicates_and_retains_pending(db):
    activate(create())
    first = claim()[0]
    observation = watches.Observation('one', (('failure:1', {'sha': 'head'}),))
    assert watches.finish_poll(first, now=101, observation=observation)
    second = claim(132)[0]
    assert watches.finish_poll(second, now=133, observation=observation)
    assert len(rows('watch_events')) == 1
    assert rows('watch_events')[0]['disposition'] == 'pending'
    third = claim(164)[0]
    oversized = watches.Observation('must-not-advance', (('failure:2', {'log': 'a' * 65536}),))
    with pytest.raises(ValueError):
        watches.finish_poll(third, now=165, observation=oversized)
    assert rows('watches')[0]['cursor'] == 'one'
    assert len(rows('watch_events')) == 1


@pytest.mark.parametrize('action,status', [('pause', 'paused'), ('cancel', 'cancelled')])
def test_lifecycle_change_during_poll_discards_result(db, action, status):
    active = activate(create())
    poll = claim()[0]
    watches.transition_watch(active['id'], topic='squid', version=1, action=action, now=101)
    assert not watches.finish_poll(poll, now=102, observation=watches.Observation('ignored'))
    assert rows('watches')[0]['status'] == status
    assert rows('watches')[0]['cursor'] is None


def test_expiry_prevents_inflight_commit_and_future_poll(db):
    activate(create(expires_at=110))
    poll = claim()[0]
    assert not watches.finish_poll(poll, now=110, observation=watches.Observation('ignored'))
    assert claim(110) == []
    assert rows('watches')[0]['status'] == 'expired'


def test_error_backoff_preserves_cursor_then_pauses(db):
    activate(create())
    now = 100
    for attempt in range(5):
        poll = claim(now)[0]
        watches.finish_poll(poll, now=now + 1, error='TimeoutError')
        row = rows('watches')[0]
        assert row['failures'] == attempt + 1
        assert row['cursor'] is None
        assert row['next_poll'] > now + 30
        now = row['next_poll']
    assert row['status'] == 'paused'
    assert claim(now) == []


def test_loop_bounds_parallelism_and_closed_watch_stops(db):
    for index in range(5):
        activate(create(request_key=str(index)))
    class Adapter:
        running = 0
        peak = 0
        async def poll(self, watch):
            self.running += 1
            self.peak = max(self.peak, self.running)
            await asyncio.sleep(0)
            self.running -= 1
            return watches.Observation('closed', closed=True)
    adapter = Adapter()
    async def run():
        loop = watches.WatchLoop({'fake': adapter}, concurrency=2, clock=lambda: 100)
        assert await loop.tick() == 2
        assert await loop.tick() == 2
        assert await loop.tick() == 1
        assert await loop.tick() == 0
    asyncio.run(run())
    assert adapter.peak == 2
    assert all(row['status'] == 'completed' for row in rows('watches'))


def test_loop_timeout_and_error_redaction(db):
    activate(create())
    class Adapter:
        async def poll(self, watch):
            raise RuntimeError('secret bearer token')
    asyncio.run(watches.WatchLoop({'fake': Adapter()}, clock=lambda: 100).tick())
    assert rows('watches')[0]['last_error'] == 'RuntimeError'
    class SlowAdapter:
        async def poll(self, watch):
            await asyncio.Event().wait()
    now = rows('watches')[0]['next_poll']
    asyncio.run(watches.WatchLoop({'fake': SlowAdapter()}, timeout=.01, clock=lambda: now).tick())
    assert rows('watches')[0]['last_error'] == 'TimeoutError'


def test_cancellation_leaves_recoverable_claim(db):
    activate(create())
    async def run():
        started = asyncio.Event()
        class Adapter:
            async def poll(self, watch):
                started.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(watches.WatchLoop({'fake': Adapter()}, clock=lambda: 100).tick())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert rows('watches')[0]['claim_token'] is not None
    assert claim(161)


def test_long_poll_interval_never_shortened(db):
    activate(create(poll_interval=7200))
    poll = claim()[0]
    watches.finish_poll(poll, now=101, observation=watches.Observation('ok'))
    assert rows('watches')[0]['next_poll'] == 7301


def test_cursor_and_events_rollback_together_on_db_error(db):
    activate(create())
    poll = claim()[0]
    conn = stats_db._connect()
    try:
        conn.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON watch_events
            WHEN NEW.event_key='bad' BEGIN SELECT RAISE(ABORT,'test failure'); END""")
        conn.commit()
    finally:
        conn.close()
    observation = watches.Observation('must-not-advance', (('good', {}), ('bad', {})))
    with pytest.raises(sqlite3.IntegrityError):
        watches.finish_poll(poll, now=101, observation=observation)
    assert rows('watch_events') == []
    assert rows('watches')[0]['cursor'] is None


def test_simultaneous_claimers_do_not_share_watch(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    activate(create())
    barrier = Barrier(2)
    def compete():
        barrier.wait()
        return claim()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: compete(), range(2)))
    assert sorted([len(first), len(second)]) == [0, 1]


def test_absent_cursor_preserves_saved_position(db):
    activate(create())
    watches.finish_poll(claim()[0], now=101, observation=watches.Observation('saved'))
    watches.finish_poll(claim(132)[0], now=133, observation=watches.Observation(None))
    assert rows('watches')[0]['cursor'] == 'saved'


def test_resume_resets_failure_budget_and_records_pause_reason(db):
    activate(create())
    now = 100
    for _ in range(5):
        watches.finish_poll(claim(now)[0], now=now, error='TimeoutError')
        now = rows('watches')[0]['next_poll']
    row = rows('watches')[0]
    assert row['pause_reason'] == 'poll_failures'
    resumed = watches.transition_watch(row['id'], topic='squid', version=row['version'],
                                       action='resume', now=now)
    assert (resumed['failures'], resumed['last_error'], resumed['pause_reason']) == (0, None, None)
    watches.finish_poll(claim(now)[0], now=now, error='TimeoutError')
    row = rows('watches')[0]
    assert row['status'] == 'active' and row['failures'] == 1
    paused = watches.transition_watch(row['id'], topic='squid', version=row['version'],
                                      action='pause', now=now)
    assert paused['pause_reason'] == 'manual'


@pytest.mark.parametrize('action', ['activate', 'pause', 'resume', 'cancel'])
def test_keyed_transition_retry_returns_original_result_after_later_changes(db, action):
    row = create()
    if action != 'activate':
        row = activate(row)
    if action == 'resume':
        row = watches.transition_watch(row['id'], topic='squid', version=row['version'],
                                        action='pause', now=101)
    args = dict(topic='squid', version=row['version'], action=action, now=102, operation_key='op1')
    first = watches.transition_watch(row['id'], **args)
    # Model lost-response retries and subsequent state changes.
    assert watches.transition_watch(row['id'], **args) == first
    if action != 'cancel':
        watches.transition_watch(row['id'], topic='squid', version=first['version'], action='cancel', now=103)
    assert watches.transition_watch(row['id'], **args) == first
    assert rows('watches')[0]['status'] == 'cancelled'
    with pytest.raises(ValueError, match='different parameters'):
        watches.transition_watch(row['id'], **(args | {'version': row['version'] + 1}))
    with pytest.raises(ValueError, match='not found'):
        watches.transition_watch(row['id'], **(args | {'topic': 'other'}))


def test_stale_unkeyed_request_rejected_after_intervening_transitions(db):
    row = activate(create())
    paused = watches.transition_watch(row['id'], topic='squid', version=row['version'], action='pause', now=101)
    watches.transition_watch(row['id'], topic='squid', version=paused['version'], action='resume', now=102)
    with pytest.raises(ValueError, match='stale'):
        watches.transition_watch(row['id'], topic='squid', version=0, action='activate', now=103)


@pytest.mark.parametrize('setup', ['empty', 'future', 'leased', 'paused'])
def test_idle_claim_never_takes_write_lock(db, monkeypatch, setup):
    if setup != 'empty':
        row = activate(create())
        if setup == 'leased':
            claim()
        elif setup == 'future':
            watches.finish_poll(claim()[0], now=100, observation=watches.Observation(None))
        else:
            watches.transition_watch(row['id'], topic='squid', version=row['version'], action='pause', now=100)
    original = watches._connect
    statements = []
    def connect():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(watches, '_connect', connect)
    assert claim() == []
    assert not any(s.startswith(('BEGIN', 'UPDATE', 'INSERT')) for s in statements)


def test_expired_creation_key_stays_reserved(db):
    row = create(expires_at=101)
    claim(102)
    assert create(expires_at=101, now=102)['id'] == row['id']
    assert create(expires_at=101, now=102)['status'] == 'expired'
    assert create(request_key='new', expires_at=200)['id'] != row['id']


def test_poll_error_log_redacts_message_and_identifies_watch(db, caplog):
    row = activate(create())
    class Adapter:
        async def poll(self, watch):
            raise RuntimeError('bearer SUPER_SECRET')
    asyncio.run(watches.WatchLoop({'fake': Adapter()}, clock=lambda: 100).tick())
    assert row['id'] in caplog.text and 'RuntimeError' in caplog.text
    assert 'SUPER_SECRET' not in caplog.text


def test_legacy_schema_migrates_pause_reason(db):
    row = activate(create())
    conn = stats_db._connect()
    try:
        conn.execute('ALTER TABLE watches DROP COLUMN pause_reason')
        conn.commit()
    finally:
        conn.close()
    conn = stats_db._connect()
    try:
        conn.execute("UPDATE watches SET status='paused',failures=5 WHERE id=?", (row['id'],))
        conn.commit()
    finally:
        conn.close()
    stats_db.init_db()
    stats_db.init_db()
    assert rows('watches')[0]['pause_reason'] == 'poll_failures'


def test_creation_replay_after_expiry_still_checks_parameters(db):
    row = create(expires_at=101)
    assert create(expires_at=101, now=102)['id'] == row['id']
    with pytest.raises(ValueError, match='different watch parameters'):
        create(expires_at=101, now=102, target='other')
    with pytest.raises(ValueError, match='future expiry'):
        create(expires_at=101, now=102, request_key='new')
    assert len(rows('watches')) == 1


def test_tick_waits_for_siblings_after_persistence_failure(monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        failed = asyncio.Event()
        started = asyncio.Event()
        release = asyncio.Event()
        batches = iter([[{'id': 'fail', 'adapter': 'fake'},
                         {'id': 'slow', 'adapter': 'fake'}], []])
        claim_count = 0
        def claims(**kwargs):
            nonlocal claim_count
            claim_count += 1
            return next(batches)
        def finish(watch, **kwargs):
            if watch['id'] == 'fail':
                if kwargs.get('error'):
                    loop.call_soon_threadsafe(failed.set)
                raise sqlite3.OperationalError('persistence failed')
            return True
        class Adapter:
            async def poll(self, watch):
                if watch['id'] == 'slow':
                    started.set()
                    await release.wait()
                return watches.Observation(None)
        monkeypatch.setattr(watches, 'claim_due', claims)
        monkeypatch.setattr(watches, 'finish_poll', finish)
        worker = watches.WatchLoop({'fake': Adapter()}, concurrency=2)
        first = asyncio.create_task(worker.tick())
        second = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(failed.wait(), 2)
            # The failure has occurred, but the first tick must retain its
            # lock until the other poll finishes; a second tick cannot claim.
            done, _ = await asyncio.wait([first], timeout=.05)
            assert not done
            second = asyncio.create_task(worker.tick())
            done, _ = await asyncio.wait([second], timeout=.05)
            assert not done and claim_count == 1
            release.set()
            with pytest.raises(sqlite3.OperationalError):
                await first
            assert await second == 0
            assert claim_count == 2
        finally:
            release.set()
            await asyncio.gather(*(task for task in (first, second) if task is not None),
                                 return_exceptions=True)
    asyncio.run(run())


def test_default_expiry_retry_does_not_extend_window(db):
    args = dict(topic='squid', adapter='fake', target='task', request_key='default', poll_interval=30)
    first = watches.create_watch(**args, now=100)
    assert first['expires_at'] == 100 + 86400
    assert watches.create_watch(**args, now=200)['expires_at'] == first['expires_at']


def test_generic_probe_events_survive_loop_restart(db):
    from agent.watch_probes import Probe, ProbeAdapter
    activate(create(adapter='corp'))
    async def call(server, tool, arguments):
        return {'revision': 'comment:edited', 'changed': True, 'summary': 'handle feedback'}
    def loop(now):
        return watches.WatchLoop({'corp': ProbeAdapter(Probe(server='corp', tool='snapshot'), call_tool=call)}, clock=lambda: now)
    asyncio.run(loop(100).tick())
    stats_db.init_db()
    asyncio.run(loop(131).tick())
    assert len(rows('watch_events')) == 1
    assert rows('watch_events')[0]['disposition'] == 'pending'
    assert rows('watches')[0]['cursor'] == 'comment:edited'
