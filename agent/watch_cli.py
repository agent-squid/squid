"""Instance-explicit JSON watch client; no home-config discovery."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

import httpx

from .watch_urls import valid_api_url
from .watch_auth import file_token


def main(argv):
    parser = argparse.ArgumentParser(prog='agentsquid ' + argv[0],
        description='Watch monitoring and explicitly granted local work. Publication is unavailable.',
        epilog='Use agentsquid watch --help for commands. SQUID_API_URL must name the issuing instance. '
               'Agent calls use SQUID_TURN_TOKEN; --human-topic uses SQUID_WATCH_CONTROL_TOKEN or ~/.squid/watch-control.token. '
               'Mutation input is a JSON object: create needs target, request_key and plan (or legacy adapter); '
               'validate needs target and plan; tools --discover lists durable MCP read tools; '
               'lifecycle actions need version and optionally operation_key/grant_id; '
               'grant needs expires_at; revoke needs grant_id; execution-grant needs exactly one of agent/flow_route, instructions, '
               'max_runs, max_attempts, request_key; execution-revoke needs grant_id. runs lists durable outcomes.')
    parser.add_argument('--api-url', help='Explicit Squid instance URL (overrides SQUID_API_URL)')
    if argv[0] == 'tools':
        parser.add_argument('--discover', action='store_true', help='Discover durable read tools and input schemas for observation plans')
        parser.add_argument('--human-topic', help='Use separate human control authentication for this topic')
        parsed = parser.parse_args(argv[1:])
        action = 'observation-tools' if parsed.discover else 'tools'
        args = parsed
    else:
        parser.add_argument('action', choices=['validate', 'create', 'list', 'show', 'activate', 'pause', 'resume', 'cancel', 'grant', 'revoke', 'execution-grant', 'execution-revoke', 'runs'])
        parser.add_argument('watch_id', nargs='?')
        parser.add_argument('--input', help='JSON file, or - for stdin')
        parser.add_argument('--human-topic', help='Use separate human control authentication for this topic')
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--offset', type=int, default=0)
        args = parser.parse_args(argv[1:])
        parsed = args
        action = args.action
    try:
        url = parsed.api_url or os.environ.get('SQUID_API_URL', '')
        if not valid_api_url(url):
            raise ValueError()
        human = args and args.human_topic is not None
        token = os.environ.get('SQUID_WATCH_CONTROL_TOKEN' if human else 'SQUID_TURN_TOKEN', '')
        if human and not token:
            token = file_token()
        if not token or len(token) > 512:
            raise ValueError()
        path = '/watch-tools' if action == 'observation-tools' else '/tools' if action == 'tools' else '/watches'
        if human:
            path = '/watch-control/' + quote(args.human_topic, safe='') + path
        if action not in {'create', 'list', 'tools', 'observation-tools', 'validate'}:
            if not args.watch_id:
                raise ValueError()
            path += '/' + quote(args.watch_id, safe='')
            if action != 'show':
                path += '/' + action
        if action in {'grant', 'revoke', 'execution-grant', 'execution-revoke'} and not human:
            raise ValueError()
        if action == 'validate':
            path += '/validate'
        method = 'GET' if action in {'list', 'show', 'tools', 'observation-tools', 'runs'} else 'POST'
        body = None
        if method == 'POST':
            if not args.input:
                raise ValueError()
            body = json.loads(sys.stdin.read() if args.input == '-' else Path(args.input).read_text())
            if not isinstance(body, dict):
                raise ValueError()
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = client.request(method, url.rstrip('/') + path,
                headers={'Authorization': 'Bearer ' + token}, json=body,
                params={'limit': args.limit, 'offset': args.offset} if action in {'list', 'runs'} else None)
        if response.status_code >= 300:
            from .watch_plans import PLAN_ERRORS
            try:
                code = response.json().get('error')
            except (ValueError, AttributeError):
                code = None
            detail = {'error': 'watch_request_failed', 'status': response.status_code}
            if isinstance(code, str) and code in PLAN_ERRORS:
                detail.update(error=code, hint=PLAN_ERRORS[code])
            print(json.dumps(detail), file=sys.stderr)
            return 1
        print(json.dumps(response.json()))
        return 0
    except (ValueError, OSError, httpx.HTTPError):
        print('{"error":"watch_client_error"}', file=sys.stderr)
        return 1
