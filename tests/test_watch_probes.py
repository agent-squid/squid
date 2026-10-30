import asyncio
import sys

import pytest

from agent.watch_probes import Probe, ProbeAdapter, normalize


def test_baseline_changes_and_replays():
    assert normalize({'revision': 'a'}, cursor=None).events == ()
    result = {'revision': 'b', 'summary': 'new comment'}
    observation = normalize(result, cursor='a')
    assert len(observation.events) == 1
    assert observation == normalize(result, cursor='a')
    assert normalize(result, cursor='b').events == ()
    assert normalize(result | {'changed': False}, cursor='a').events == ()
    assert normalize(result | {'changed': True}, cursor=None).events
    assert normalize(result | {'closed': True}, cursor='a').closed


@pytest.mark.parametrize('result', [None, {}, {'revision': ''}, {'revision': 'a', 'changed': 1},
    {'revision': 'a', 'closed': 'false'}, {'revision': 'a', 'summary': []},
    {'revision': 'a', 'unknown': True}, {'revision': 'a' * 4097}])
def test_malformed_results_fail(result):
    with pytest.raises(ValueError):
        normalize(result, cursor=None)


def test_executable_uses_json_stdin_without_shell():
    script = 'import json,sys; r=json.load(sys.stdin); print(json.dumps({"revision":r["target"],"changed":True}))'
    adapter = ProbeAdapter(Probe(argv=(sys.executable, '-c', script)))
    result = asyncio.run(adapter.poll({'target': '$(touch forbidden); literal', 'cursor': None}))
    assert result.cursor == '$(touch forbidden); literal'
    assert len(result.events) == 1


@pytest.mark.parametrize('script,error', [
    ('print("x" * 10000)', ValueError),
    ('import sys; sys.exit(1)', RuntimeError),
    ('print("not json")', ValueError),
    ('import time; time.sleep(10)', asyncio.TimeoutError),
])
def test_executable_failures_bounded(script, error):
    adapter = ProbeAdapter(Probe(argv=(sys.executable, '-c', script), timeout=.2, output_limit=100))
    with pytest.raises(error):
        asyncio.run(adapter.poll({'target': 'task', 'cursor': None}))


def test_mcp_uses_registered_server_tool_and_same_contract():
    calls = []
    async def call(server, tool, arguments):
        calls.append((server, tool, arguments))
        return {'revision': 'edited', 'summary': 'updated comment'}
    adapter = ProbeAdapter(Probe(server='corp', tool='task_snapshot'), call_tool=call)
    observation = asyncio.run(adapter.poll({'target': 'LIN-123', 'cursor': 'old'}))
    assert calls == [('corp', 'task_snapshot', {'target': 'LIN-123', 'cursor': 'old'})]
    assert observation.cursor == 'edited' and observation.events


@pytest.mark.parametrize('kwargs', [{}, {'argv': ('bin',), 'server': 'corp', 'tool': 'read'},
    {'server': 'corp'}, {'argv': ('bin',), 'timeout': float('nan')},
    {'argv': ('bin',), 'output_limit': 0}])
def test_invalid_registrations(kwargs):
    with pytest.raises(ValueError):
        Probe(**kwargs)


@pytest.mark.parametrize('leader_exits', [False, True])
def test_timeout_kills_descendants_holding_stdout(tmp_path, leader_exits):
    import os
    import time
    marker = tmp_path / 'child-survived'
    child = f'import time,pathlib; time.sleep(1); pathlib.Path({str(marker)!r}).touch()'
    script = ('import subprocess,sys,time; '
              f'subprocess.Popen([sys.executable,"-c",{child!r}]); '
              + ('sys.exit(0)' if leader_exits else 'time.sleep(10)'))
    adapter = ProbeAdapter(Probe(argv=(sys.executable, '-c', script), timeout=.3))
    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(adapter.poll({'target': 'task', 'cursor': None}))
    assert time.monotonic() - start < .9
    time.sleep(1)
    assert not marker.exists()


def test_event_limit_uses_storage_serialization():
    import json
    from agent.watches import encode_event_payload
    result = {'revision': 'r', 'changed': True, 'summary': ''}
    overhead = len(json.dumps(result).encode())
    result['summary'] = 'x' * (65536 - overhead)
    observation = normalize(result, cursor=None, output_limit=1048576)
    assert len(encode_event_payload(observation.events[0][1]).encode()) == 65536
    result['summary'] += 'x'
    with pytest.raises(ValueError, match='oversized watch event'):
        normalize(result, cursor=None, output_limit=1048576)
    # Even the default compact probe encoding fits, while storage encoding does not.
    assert len(json.dumps(result, separators=(',', ':')).encode()) <= 65536
    with pytest.raises(ValueError, match='oversized watch event'):
        normalize(result, cursor=None)


def test_detached_descendant_cannot_hold_cleanup_open():
    import time
    # This child escapes the process group but exits on its own. Cleanup must
    # close its inherited pipe rather than wait for its whole lifetime.
    script = ('import subprocess,sys,time; subprocess.Popen('
              '[sys.executable,"-c","import time; time.sleep(2)"], '
              'start_new_session=True); time.sleep(10)')
    adapter = ProbeAdapter(Probe(argv=(sys.executable, '-c', script), timeout=.3))
    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(adapter.poll({'target': 'task', 'cursor': None}))
    assert time.monotonic() - start < 1.8
    time.sleep(1)  # Let the deliberately detached fixture exit before returning.


@pytest.mark.parametrize('cancel', [False, True])
def test_interrupted_error_cleanup_closes_transport(tmp_path, monkeypatch, cancel):
    import os
    import signal
    from agent import watch_probes

    pid_file = tmp_path / 'detached.pid'
    script = ('import subprocess,sys,pathlib,time; '
              'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],'
              'start_new_session=True); '
              f'pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); '
              'print("x"*10000,flush=True); time.sleep(30)')
    processes = []
    create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(watch_probes.asyncio, 'create_subprocess_exec', capture)

    async def run():
        adapter = ProbeAdapter(Probe(argv=(sys.executable, '-c', script),
                                     timeout=10 if cancel else .5, output_limit=100))
        task = asyncio.create_task(adapter.poll({'target': 'task', 'cursor': None}))
        try:
            # The size error kills the leader, then cleanup waits on the pipe
            # held by the detached child. Interrupt that cleanup by either path.
            async def wait_for_cleanup():
                while not processes or processes[0].returncode is None:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait_for_cleanup(), 3)
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else asyncio.TimeoutError):
                await task
            process = processes[0]
            assert process._transport.is_closing()
            assert process._transport.get_pipe_transport(1).is_closing()
            await asyncio.wait_for(process.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    asyncio.run(run())


def test_loop_rejects_probe_without_cleanup_budget():
    from agent.watches import WatchLoop
    adapter = ProbeAdapter(Probe(argv=('probe',), timeout=29))
    with pytest.raises(ValueError, match='cleanup allowance'):
        WatchLoop({'probe': adapter}, timeout=30)
    assert WatchLoop({'probe': adapter}, timeout=31)


def test_recurring_status_with_distinct_source_attempts():
    cursor = None
    keys = []
    for attempt, status in enumerate(('fail', 'pass', 'fail', 'pass')):
        observation = normalize({'revision': f'run:1:attempt:{attempt}',
                                 'changed': True, 'summary': status}, cursor=cursor)
        keys.append(observation.events[0][0])
        cursor = observation.cursor
    assert len(set(keys)) == 4
    # Replaying a previously observed source attempt keeps its original identity.
    replay = normalize({'revision': 'run:1:attempt:0', 'changed': True,
                        'summary': 'fail'}, cursor=cursor)
    assert replay.events[0][0] == keys[0]
