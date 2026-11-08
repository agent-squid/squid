"""Structured publication intent only. Recording a scope does not enable publishing."""
from pathlib import Path
import re
import subprocess

from .admission_reads import local_git_read


@local_git_read
def _git(repository, *args):
    result = subprocess.run(['git', '-C', str(repository), *args], capture_output=True,
                            text=True, timeout=10)
    if result.returncode:
        raise ValueError('invalid publication repository')
    return result.stdout.strip()


def capture(body, topic, agent):
    from .continuations import current_scope
    from .worktree import repo_root_for
    if (not isinstance(body, dict)
            or set(body) != {'repository', 'remote', 'base_branch', 'push', 'pull_request'}
            or body['push'] is not True or body['pull_request'] is not True
            or any(not isinstance(body[k], str) or not body[k].strip()
                   or len(body[k]) > 4096 for k in ('repository', 'remote', 'base_branch'))
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', body['remote'])
            or body['base_branch'].startswith('-')):
        raise ValueError('invalid publication scope')
    repository = Path(body['repository']).expanduser().resolve()
    configured = current_scope(topic, agent)
    roots = configured['code_roots']
    # Single repository only, explicitly chosen from the granted code roots.
    if repository not in {repo_root_for(root) for root in roots}:
        raise ValueError('publication repository outside code roots')
    if Path(_git(repository, 'rev-parse', '--show-toplevel')).resolve() != repository:
        raise ValueError('invalid publication repository')
    _git(repository, 'check-ref-format', 'refs/heads/' + body['base_branch'])
    fetch_urls = _git(repository, 'remote', 'get-url', '--all', body['remote']).splitlines()
    push_urls = _git(repository, 'remote', 'get-url', '--push', '--all', body['remote']).splitlines()
    if len(fetch_urls) != 1 or len(push_urls) != 1:
        raise ValueError('publication requires one remote URL')
    return dict(body, repository=str(repository),
                git_common_dir=str(Path(_git(repository, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()),
                fetch_url=fetch_urls[0], push_url=push_urls[0], execution_scope=configured)


def matches(encoded, topic, agent):
    import json
    try:
        saved = json.loads(encoded)
        body = {key: saved[key] for key in ('repository', 'remote', 'base_branch', 'push', 'pull_request')}
        return saved == capture(body, topic, agent)
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return False
