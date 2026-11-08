"""Exercise installer recovery without downloading or changing the user's home."""
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'bin/install.sh'
SOURCE = SCRIPT.read_text()
HELPERS = SOURCE[SOURCE.index('need_version()'):SOURCE.index('ERRORS=0')]


class InstallScriptTests(unittest.TestCase):
    def run_shell(self, script, answer=None, terminal=False, env=None):
        master = slave = None
        if terminal:
            master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                ['bash', '-euo', 'pipefail', '-c', HELPERS + '\n' + script],
                stdin=slave if terminal else subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            if terminal:
                os.close(slave)
                slave = None
                os.write(master, answer)
            out, err = process.communicate(None if terminal else answer, timeout=10)
            return process.returncode, out.decode(), err.decode()
        finally:
            if slave is not None:
                os.close(slave)
            if master is not None:
                os.close(master)

    def test_version_rejects_failed_or_malformed_commands(self):
        for body in ['echo "Python 3.13.9"; exit 1', 'echo broken', 'echo "tool 3.13"']:
            with self.subTest(body=body):
                code, _, err = self.run_shell(
                    'fake_python() { ' + body + '; }; if need_version fake_python 3 10; then exit 9; fi')
                self.assertEqual(code, 0, err)
        code, _, err = self.run_shell(
            'fake_python() { echo "Python 3.13.9"; }; need_version fake_python 3 10')
        self.assertEqual(code, 0, err)

    def test_confirmation(self):
        cases = [(b'\n', True, True), (b'y\n', True, True),
                 (b'n\n', True, False), (b'\x04', True, False),
                 (b'y\n', False, False)]
        for answer, terminal, accepted in cases:
            with self.subTest(answer=answer, terminal=terminal):
                code, out, err = self.run_shell(
                    'if confirm "Install?"; then echo accepted; else echo declined; fi',
                    answer, terminal)
                self.assertEqual(code, 0, err)
                self.assertEqual(out.strip(), 'accepted' if accepted else 'declined')

    def test_recovery_verifies_interpreter_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / 'python'
            python.write_text('#!/bin/bash\necho "Python 3.13.9"\n')
            python.chmod(0o755)
            uv = root / 'uv'
            uv.write_text('''#!/bin/bash
case "$1 $2" in
  "python install") [[ "$FAIL_INSTALL" != 1 ]];;
  "python find") echo "$FOUND_PYTHON";;
  *) exit 1;;
esac
''')
            uv.chmod(0o755)
            for fail, found, expected in [('0', str(python), True),
                                           ('1', str(python), False),
                                           ('0', '/missing/python', False)]:
                with self.subTest(fail=fail, found=found):
                    env = {**os.environ, 'FAIL_INSTALL': fail, 'FOUND_PYTHON': found,
                           'TEST_UV': str(uv)}
                    code, out, err = self.run_shell(
                        'UV="$TEST_UV"; PYTHON=""; ok() { :; }; '
                        'install_python || true; printf "%s" "$PYTHON"', b'y\n', True, env)
                    self.assertEqual(code, 0, err)
                    self.assertEqual(out, str(python) if expected else '')

    def test_uv_bootstrap_without_executable_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'HOME': directory}
            code, out, err = self.run_shell('''
UV=""; PYTHON=""
curl() { return 0; }
env() { return 0; }
ok() { echo unexpected-success; }
install_python || true
printf 'uv=%s,python=%s' "$UV" "$PYTHON"
''', b'y\n', True, env)
            self.assertEqual(code, 0, err)
            self.assertEqual(out, 'uv=,python=')

    def test_tailscale_setup_uses_virtualenv_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            (root / 'bin/python3').write_text('#!/bin/bash\necho venv-python\n')
            (root / 'bin/python3').chmod(0o755)
            (root / 'bin/tailscale-serve.sh').write_text('python3\n')
            line = next(line for line in SOURCE.splitlines() if 'source "$SQUID_DIR/bin/tailscale-serve.sh"' in line)
            code, out, err = self.run_shell(
                'VENV_DIR="$TEST_ROOT"; SQUID_DIR="$TEST_ROOT"\n' + line,
                env={**os.environ, 'TEST_ROOT': directory})
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), 'venv-python')

    def test_existing_virtualenv_is_found_without_path_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.venv/bin').mkdir(parents=True)
            python = root / '.venv/bin/python'
            python.write_text('#!/bin/bash\necho "Python 3.13.9"\n')
            python.chmod(0o755)
            detection = SOURCE[SOURCE.index('PYTHON=""'):SOURCE.index('# uv-managed')]
            code, out, err = self.run_shell(
                'SQUID_DIR="$TEST_ROOT"\n' + detection + '\nprintf "%s" "$PYTHON"',
                env={**os.environ, 'TEST_ROOT': directory})
            self.assertEqual(code, 0, err)
            self.assertEqual(out, str(python))

    def test_broken_virtualenv_is_recreated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / '.venv'
            (venv / 'bin').mkdir(parents=True)
            (venv / 'bin/python').symlink_to('/missing/interpreter')
            (venv / 'stale-marker').touch()
            # Stub only virtualenv creation; exercise the actual validation and
            # cleanup block with a genuinely broken interpreter symlink.
            start = SOURCE.index('  VENV_DIR="$SQUID_DIR/.venv"')
            end = SOURCE.index('  if ! "$VENV_DIR/bin/python" -m pip', start)
            code, out, err = self.run_shell('''
SQUID_DIR="$TEST_ROOT"; PYTHON=/replacement/python
warn() { :; }
run_quiet() { [[ "$2" == /replacement/python ]] || exit 9; echo recreated; }
''' + SOURCE[start:end], env={**os.environ, 'TEST_ROOT': directory})
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), 'recreated')
            self.assertFalse((venv / 'stale-marker').exists())
