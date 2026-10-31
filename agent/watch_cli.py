"""Instance-explicit JSON watch client; no home-config discovery."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

import httpx

from .watch_urls import valid_api_url


def main(argv):
    parser = argparse.ArgumentParser(prog='agentsquid ' + argv[0],
        description='Observation-only watch operations. Polling and automatic turns are not enabled.',
        epilog='Use agentsquid watch --help for commands. SQUID_API_URL must name the issuing instance. '
               'Agent calls use SQUID_TURN_TOKEN; --human-topic uses SQUID_WATCH_CONTROL_TOKEN. '
               'Mutation input is a JSON object: create needs adapter, target, request_key; '
               'lifecycle actions need version and optionally operation_key/grant_id; '
               'grant needs expires_at; revoke needs grant_id.')
    if argv[0] == 'tools':
        parser.parse_args(argv[1:])
        action = 'tools'
        args = None
    else:
        parser.add_argument('action', choices=['create', 'list', 'show', 'activate', 'pause', 'resume', 'cancel', 'grant', 'revoke'])
        parser.add_argument('watch_id', nargs='?')
        parser.add_argument('--input', help='JSON file, or - for stdin')
        parser.add_argument('--human-topic', help='Use separate human control authentication for this topic')
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--offset', type=int, default=0)
        args = parser.parse_args(argv[1:])
        action = args.action
    try:
        url = os.environ.get('SQUID_API_URL', '')
        if not valid_api_url(url):
            raise ValueError()
        human = args and args.human_topic is not None
        token = os.environ.get('SQUID_WATCH_CONTROL_TOKEN' if human else 'SQUID_TURN_TOKEN', '')
        if not token or len(token) > 512:
            raise ValueError()
        path = '/tools' if action == 'tools' else '/watches'
        if human:
            path = '/watch-control/' + quote(args.human_topic, safe='') + path
        if action not in {'create', 'list', 'tools'}:
            if not args.watch_id:
                raise ValueError()
            path += '/' + quote(args.watch_id, safe='')
            if action != 'show':
                path += '/' + action
        if action in {'grant', 'revoke'} and not human:
            raise ValueError()
        method = 'GET' if action in {'list', 'show', 'tools'} else 'POST'
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
                params={'limit': args.limit, 'offset': args.offset} if action == 'list' else None)
        if response.status_code >= 300:
            print(json.dumps({'error': 'watch_request_failed', 'status': response.status_code}), file=sys.stderr)
            return 1
        print(json.dumps(response.json()))
        return 0
    except (ValueError, OSError, httpx.HTTPError):
        print('{"error":"watch_client_error"}', file=sys.stderr)
        return 1
