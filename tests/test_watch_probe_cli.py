import json
import stat
import sys

import pytest
import yaml

from agent import watch_cli


def registration(tmp_path, code, **options):
    path = tmp_path / 'probe.json'
    path.write_text(json.dumps({'argv': [sys.executable, '-c', code], **options}))
    return str(path)


def test_contract_without_instance_credentials(monkeypatch, capsys):
    monkeypatch.delenv('SQUID_API_URL', raising=False)
    assert watch_cli.main(['watch', 'probe', 'contract']) == 0
    contract = json.loads(capsys.readouterr().out)
    assert contract['stdin']['cursor'] is None
    assert 'watches.registry.probes' in contract['registration']['location']


def test_real_wrapper_baseline_and_revision_change(tmp_path, capsys):
    path = registration(tmp_path, 'import json,sys; r=json.load(sys.stdin); print(json.dumps({"revision": r["target"]}))')
    args = ['watch', 'probe', 'test', '--input', path, '--target', 'v2']
    assert watch_cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['request'] == {'target': 'v2', 'cursor': None}
    assert first['observation']['events'] == []
    assert watch_cli.main(args + ['--cursor', 'v1']) == 0
    changed = json.loads(capsys.readouterr().out)
    assert changed['observation']['events'][0][1]['revision'] == 'v2'
    assert watch_cli.main(args + ['--cursor', 'v2']) == 0
    assert json.loads(capsys.readouterr().out)['observation']['events'] == []


@pytest.mark.parametrize('code,options', [
    ('print("secret-invalid-json")', {}),
    ('raise SystemExit("secret-provider-error")', {}),
    ('print(\'{"revision":"v1", "unknown":"secret"}\')', {}),
    ('print("secret" * 100)', {'output_limit': 10}),
    ('import time; time.sleep(10)', {'timeout': 0.05}),
])
def test_failures_are_bounded_and_sanitized(tmp_path, capsys, code, options):
    path = registration(tmp_path, code, **options)
    assert watch_cli.main(['watch', 'probe', 'test', '--input', path, '--target', 'x']) == 1
    result = capsys.readouterr()
    assert result.out == ''
    assert json.loads(result.err) == {'error': 'watch_probe_test_failed'}


def test_registration_does_not_execute_or_enable_and_refuses_overwrite(tmp_path, capsys):
    marker = tmp_path / 'executed'
    path = registration(tmp_path, f'open({str(marker)!r}, "w").close()')
    config = tmp_path / 'squid.yaml'
    config.write_text('port: 8123\nwatches:\n  enabled: false\n')
    config.chmod(0o600)
    args = ['watch', 'probe', 'register', '--input', path, '--config', str(config), '--name', 'snapshot']
    assert watch_cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['restart_required'] is True
    saved = yaml.safe_load(config.read_text())
    assert saved['port'] == 8123
    assert saved['watches']['enabled'] is False
    assert saved['watches']['registry']['probes']['snapshot']['argv'][0] == sys.executable
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert not marker.exists()
    original = config.read_bytes()
    assert watch_cli.main(args) == 1
    assert config.read_bytes() == original


@pytest.mark.parametrize('contents,name', [('watches: [', 'snapshot'), ('port: 8123', 'plan'), ('port: 8123', 'bad/name')])
def test_invalid_config_or_name_leaves_file_unchanged(tmp_path, capsys, contents, name):
    path = registration(tmp_path, 'pass')
    config = tmp_path / 'squid.yaml'
    config.write_text(contents)
    assert watch_cli.main(['watch', 'probe', 'register', '--input', path, '--config', str(config), '--name', name]) == 1
    assert config.read_text() == contents
    assert json.loads(capsys.readouterr().err)['error'] == 'watch_probe_register_failed'


@pytest.mark.parametrize('other_writer', ['registration', 'config_editor'])
def test_concurrent_config_writers_serialize(tmp_path, other_writer):
    """Pause registration at replace; a second process must wait for its lock."""
    import os
    from pathlib import Path
    import subprocess
    import selectors

    def expect_line(process, expected):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), f"Timed out waiting for {expected!r}"
        assert process.stdout.readline().strip() == expected

    path = registration(tmp_path, 'pass')
    config = tmp_path / 'squid.yaml'
    config.write_text('port: 8123\n')
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, 'PYTHONPATH': root}
    first_script = '''
import sys
from agent import watch_probe_cli as cli
replace = cli.os.replace
def paused_replace(source, destination):
    print('ready', flush=True)
    sys.stdin.readline()
    replace(source, destination)
cli.os.replace = paused_replace
raise SystemExit(cli.main(sys.argv[1:]))
'''
    second_script = '''
import sys
from agent import watch_probe_cli as cli
print('attempting', flush=True)
raise SystemExit(cli.main(sys.argv[1:]))
'''
    second_args = ['register', '--input', path, '--config', str(config), '--name', 'second']
    if other_writer == 'config_editor':
        second_script = '''
import sys
from pathlib import Path
from agent import config
config._USER_CONFIG = Path(sys.argv[1])
revision = config.config_revision('port: 8123\\n')
print('attempting', flush=True)
try:
    config.write_config_text('port: 9999\\n', revision)
except RuntimeError:
    print('conflict', flush=True)
else:
    raise SystemExit('stale save was accepted')
'''
        second_args = [str(config)]
    first = subprocess.Popen([sys.executable, '-c', first_script, 'register', '--input', path,
                              '--config', str(config), '--name', 'first'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env)
    second = None
    try:
        expect_line(first, 'ready')
        second = subprocess.Popen([sys.executable, '-c', second_script, *second_args],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        expect_line(second, 'attempting')
        with pytest.raises(subprocess.TimeoutExpired):
            second.communicate(timeout=0.2)
        first.stdin.write('\n')
        first.stdin.flush()
        out, err = first.communicate(timeout=10)
        assert first.returncode == 0, err
        out, err = second.communicate(timeout=10)
        assert second.returncode == 0, err
        saved = yaml.safe_load(config.read_text())
        assert saved['port'] == 8123
        assert 'first' in saved['watches']['registry']['probes']
        if other_writer == 'registration':
            assert 'second' in saved['watches']['registry']['probes']
        else:
            assert out.strip() == 'conflict'
    finally:
        for process in (first, second):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)


@pytest.mark.parametrize('aliased_level', ['watches', 'registry', 'probes'])
def test_registration_preserves_unrelated_yaml_alias_values(tmp_path, capsys, aliased_level):
    path = registration(tmp_path, 'pass')
    config = tmp_path / 'squid.yaml'
    configs = {
        'watches': 'unrelated: &shared {enabled: false}\nwatches: *shared\n',
        'registry': 'unrelated: &shared {}\nwatches:\n  registry: *shared\n',
        'probes': 'unrelated: &shared {}\nwatches:\n  registry:\n    probes: *shared\n',
    }
    config.write_text(configs[aliased_level])
    unrelated = yaml.safe_load(config.read_text())['unrelated']
    assert watch_cli.main(['watch', 'probe', 'register', '--input', path,
                           '--config', str(config), '--name', 'snapshot']) == 0
    saved = yaml.safe_load(config.read_text())
    assert saved['unrelated'] == unrelated
    assert 'snapshot' in saved['watches']['registry']['probes']
