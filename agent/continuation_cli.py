"""Instance-explicit continuation client; does not load server configuration."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

import httpx

from .watch_urls import valid_api_url


def main(argv):
    parser = argparse.ArgumentParser(prog='agentsquid continue', description=
        'Schedule one bounded sequence (up to 8 steps) after this human turn succeeds and syncs. '
        'Enabled automatically with worktree.enabled: true. Watch and Flow successors use isolated local steps, read-only task_review, or explicitly granted host task_publication. '
        'Create input: {"request_key":"commit","mode":"backing_git","prompt":"Commit the requested changes"}. '
        'For multiple steps: {"request_key":"task","steps":[{"mode":"backing_git","prompt":"Commit"},{"mode":"isolated","prompt":"Next edit"}]}. '
        'Sequence steps must record their outcome with result --input using {"success":true} or {"success":false}. '
        'Keep each prompt to the next action and necessary scope; do not repeat conversation history or standard execution rules. '
        'Modes: isolated (local edits/checks), backing_git (inspect/commit and explicitly user-requested push of the current branch; no file edits, tags, releases or PRs). '
        'task_review requires a separate read-only review and positive result. task_publication must be last and requires a recorded human grant for the saved task branch; the host commits, pushes that branch and creates/updates its PR. This records future work; it does not wait for completion. No automatic retry after uncertain execution.')
    parser.add_argument('action', choices=['create', 'list', 'show', 'cancel', 'result'])
    parser.add_argument('identifier', nargs='?')
    parser.add_argument('--input', help='JSON file, or - for stdin')
    parser.add_argument('--api-url')
    args = parser.parse_args(argv)
    try:
        url = args.api_url or os.environ.get('SQUID_API_URL', '')
        token = os.environ.get('SQUID_CONTINUE_TOKEN', '')
        if not valid_api_url(url) or not token or len(token) > 512:
            raise ValueError()
        path = '/continuations'
        if args.action in {'show', 'cancel'}:
            if not args.identifier:
                raise ValueError()
            path += '/' + quote(args.identifier, safe='')
        if args.action == 'cancel':
            path += '/cancel'
        body = None
        if args.action == 'result':
            path += '/result'
        if args.action in {'create', 'result'}:
            if not args.input:
                raise ValueError()
            body = json.loads(sys.stdin.read() if args.input == '-' else Path(args.input).read_text())
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = client.request('POST' if args.action in {'create', 'cancel', 'result'} else 'GET',
                url.rstrip('/') + path, headers={'Authorization': 'Bearer ' + token}, json=body)
        if response.status_code >= 300:
            print(json.dumps({'error': 'continuation_request_failed', 'status': response.status_code}), file=sys.stderr)
            return 1
        print(json.dumps(response.json()))
        return 0
    except (ValueError, OSError, httpx.HTTPError):
        print('{"error":"continuation_client_error"}', file=sys.stderr)
        return 1
