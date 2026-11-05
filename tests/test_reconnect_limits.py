"""Reconnect bursts must leave capacity for streaming event persistence."""
import asyncio
import threading
import resource
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import server, stats_db


@pytest.mark.parametrize('soft,hard,expected', [
    (256, 4096, 4096), (256, resource.RLIM_INFINITY, 10240), (20000, 30000, None), (resource.RLIM_INFINITY, resource.RLIM_INFINITY, None),
])
def test_descriptor_headroom(monkeypatch, soft, hard, expected):
    import resource
    monkeypatch.setattr(resource, 'getrlimit', lambda _: (soft, hard))
    change = Mock()
    monkeypatch.setattr(resource, 'setrlimit', change)
    server._raise_file_descriptor_limit()
    if expected is None:
        change.assert_not_called()
    else:
        change.assert_called_once_with(resource.RLIMIT_NOFILE, (expected, hard))


def test_descriptor_limit_failure_is_nonfatal(monkeypatch):
    import resource
    monkeypatch.setattr(resource, 'getrlimit', lambda _: (256, 4096))
    monkeypatch.setattr(resource, 'setrlimit', Mock(side_effect=OSError('denied')))
    server._raise_file_descriptor_limit()


@pytest.mark.asyncio
async def test_200_revert_checks_allow_streaming_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, '_DB_PATH', tmp_path / 'squid.db')
    stats_db.init_db()
    user = stats_db.insert_user_message('burst', 'codex', 'test')
    msg = stats_db.insert_assistant_message('burst', 'codex', user)
    active = peak = 0
    lock = threading.Lock()
    release = threading.Event()

    def read(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            assert release.wait(10)
            with stats_db._connection() as conn:
                conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()
            return {'file.txt': 'revertable'}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(server, '_validate_repo_path', lambda _: tmp_path)
    monkeypatch.setattr(server, 'get_message_gitdiff', read)
    monkeypatch.setattr(server, '_worktree_diff_blocked', lambda _: None)
    monkeypatch.setattr(server, 'get_diff_revert_eligibility', read)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        diff_revert_semaphore=asyncio.Semaphore(4))))
    tasks = [asyncio.create_task(server.diff_revert_status(i, str(tmp_path), request))
             for i in range(200)]

    async def wait_for_active_reads():
        while active != 4:
            await asyncio.sleep(.001)

    try:
        await asyncio.wait_for(wait_for_active_reads(), timeout=10)
        # Persist live output while four eligibility reads occupy the cap.
        for seq in range(20):
            await asyncio.to_thread(stats_db.insert_run_event, msg, seq, 'text', 'streaming')
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert peak == 4
    assert all(response.status_code == 200 for response in results)
    with stats_db._connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM run_events WHERE msg_id=?', (msg,)).fetchone()[0] == 20
