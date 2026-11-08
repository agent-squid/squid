"""Small JSON CLI for bounded routes, with no server-config imports."""
import argparse
import json
from pathlib import Path
import sys

import httpx

from .shared_tools import request


def main(argv):
    parser = argparse.ArgumentParser(prog='agentsquid route', description=
        'Validate or schedule a bounded local-work route after successful completion and sync. '
        'Requires routes.enabled and worktree.enabled. Same-topic immediate routes only; '
        'watch, ordinary Flow, task and continuation origins cannot launch routes. '
        'Generated route members may launch nested routes or local continuations within shared root limits. '
        'launch input: {"request_key":"review","route":"#topic@agent>@review","prompt":"Review this change"}. '
        'validate input: {"route":"#topic@agent>@review"}. Scheduling does not wait for completion.')
    parser.add_argument('action', choices=['validate', 'launch', 'list', 'show', 'cancel', 'capabilities'])
    parser.add_argument('identifier', nargs='?')
    parser.add_argument('--input', help='JSON file, or - for stdin')
    parser.add_argument('--api-url')
    args = parser.parse_args(argv)
    try:
        body = {}
        if args.action in {'validate', 'launch'}:
            if not args.input:
                raise ValueError()
            body = json.loads(sys.stdin.read() if args.input == '-' else Path(args.input).read_text())
        elif args.action in {'show', 'cancel'}:
            body = dict(identifier=args.identifier)
        operation = 'capabilities' if args.action == 'capabilities' else 'route_' + args.action
        result = request(operation, body, api_url=args.api_url)
        print(json.dumps(result), file=sys.stderr if isinstance(result, dict) and 'error' in result else sys.stdout)
        return int(isinstance(result, dict) and 'error' in result)
    except (ValueError, OSError, httpx.HTTPError):
        print('{"error":"shared_client_error"}', file=sys.stderr)
        return 1
