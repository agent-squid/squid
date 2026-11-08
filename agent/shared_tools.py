"""Stable schemas and instance-explicit transport shared by CLI and native MCP."""
import os
from urllib.parse import quote

import httpx

from .watch_urls import valid_api_url


def obj(properties=None, required=()):
    return dict(type='object', properties=properties or {}, required=list(required), additionalProperties=False)


TEXT = dict(type='string', minLength=1)
IDENTIFIER = obj(dict(identifier=TEXT), ['identifier'])
ROUTE = obj(dict(route=dict(type='string', minLength=1, maxLength=2048)), ['route'])
LAUNCH = obj(dict(request_key=dict(type='string', minLength=1, maxLength=128), route=ROUTE['properties']['route'],
                  prompt=dict(type='string', minLength=1, maxLength=16000)), ['request_key', 'route', 'prompt'])
STEP = obj(dict(mode=dict(enum=['isolated', 'backing_git', 'task_review', 'task_publication']),
                prompt=dict(type='string', minLength=1, maxLength=16000)), ['mode', 'prompt'])
CREATE = dict(type='object', oneOf=[obj(dict(request_key=dict(type='string', minLength=1, maxLength=128), **STEP['properties']), ['request_key', 'mode', 'prompt']),
                    obj(dict(request_key=dict(type='string', minLength=1, maxLength=128), steps=dict(type='array', items=STEP, minItems=1, maxItems=8)), ['request_key', 'steps'])])
OPERATIONS = {
    'history_search': ('POST', '/agent-history/search', obj(dict(query=dict(type='string', minLength=1, maxLength=512), target=dict(enum=['responses', 'prompts']), scope=dict(enum=['exact', 'subtree'], default='exact'), limit=dict(type='integer', minimum=1, maximum=20)), ['query'])),
    'history_fetch': ('POST', '/agent-history/fetch', obj(dict(message_id=dict(type='integer', minimum=1, maximum=2**63 - 1), offset=dict(type='integer', minimum=0), scope=dict(enum=['exact', 'subtree'], default='exact')), ['message_id'])),
    'capabilities': ('GET', '/capabilities', obj()),
    'route_validate': ('POST', '/routes/validate', ROUTE),
    'route_launch': ('POST', '/routes', LAUNCH),
    'route_list': ('GET', '/routes', obj()),
    'route_show': ('GET', '/routes/{identifier}', IDENTIFIER),
    'route_cancel': ('POST', '/routes/{identifier}/cancel', IDENTIFIER),
    'continuation_create': ('POST', '/continuations', CREATE),
    'continuation_list': ('GET', '/continuations', obj()),
    'continuation_show': ('GET', '/continuations/{identifier}', IDENTIFIER),
    'continuation_cancel': ('POST', '/continuations/{identifier}/cancel', IDENTIFIER),
    'continuation_result': ('POST', '/continuations/result', obj(dict(success=dict(type='boolean')), ['success'])),
}


def catalog():
    return [dict(name=name, method=value[0], path=value[1], input_schema=value[2]) for name, value in OPERATIONS.items()]


def request(operation, arguments, *, api_url=None):
    method, path, _ = OPERATIONS[operation]
    url = api_url or os.environ.get('SQUID_API_URL', '')
    token = os.environ.get('SQUID_CONTINUE_TOKEN', '')
    if not valid_api_url(url) or not token or len(token) > 512 or not isinstance(arguments, dict):
        raise ValueError('shared_client_error')
    if '{identifier}' in path:
        if set(arguments) != {'identifier'} or not isinstance(arguments['identifier'], str) or not arguments['identifier']:
            raise ValueError('shared_client_error')
        path = path.replace('{identifier}', quote(arguments['identifier'], safe=''))
        arguments = {}
    if method == 'GET' and arguments:
        raise ValueError('shared_client_error')
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        response = client.request(method, url.rstrip('/') + path,
            headers={'Authorization': 'Bearer ' + token}, json=arguments if method == 'POST' else None)
    if response.status_code >= 300:
        # Server errors are codes, never reflect transport exceptions or secrets.
        try:
            code = response.json().get('error')
        except (ValueError, AttributeError):
            code = None
        safe = {'invalid_history_request', 'history_not_found', 'invalid_route_request', 'route_conflict', 'parent_not_running', 'route_not_found',
                'route_not_authorized', 'unsupported_route', 'route_depth_exhausted', 'route_turns_exhausted',
                'continuation_not_authorized', 'continuation_conflict', 'invalid_continuation_request',
                'step_not_running', 'original_scope_unavailable', 'continuation_not_found'}
        return dict(error=code if isinstance(code, str) and code in safe else 'shared_request_failed', status=response.status_code)
    return response.json()


def discover(token):
    import json
    from . import routes, continuations as c, stats_db as db
    with db._connection() as conn:
        parent, credential = routes.principal(conn, token)
        member = routes.membership(conn, parent['id'])
        if member is None:
            accepted = conn.execute('SELECT root_id,depth FROM route_requests WHERE parent_id=?', (parent['id'],)).fetchone()
            if accepted:
                member = dict(root_id=accepted['root_id'], depth=accepted['depth'] - 1)
        bounds = routes.limits(conn, member['root_id']) if member else {key: routes.settings()[key] for key in routes.DEFAULTS}
        budget = None
        if member:
            root = conn.execute('SELECT reserved_turns FROM route_roots WHERE id=?', (member['root_id'],)).fetchone()
            used = conn.execute('SELECT COALESCE(SUM(seconds),0),COALESCE(SUM(active),0) FROM route_charges WHERE root_id=?', (member['root_id'],)).fetchone()
            budget = dict(root_id=member['root_id'], depth=member['depth'], reserved_turns=root[0],
                          remaining_turns=max(0, bounds['max_turns'] - root[0]), reserved_seconds=used[0], active=used[1])
        blockers = []
        if not routes.settings()['enabled']:
            blockers.append('routes_disabled')
        from .config import WORKTREE_ISOLATION_ENABLED
        if not WORKTREE_ISOLATION_ENABLED:
            blockers.append('isolation_disabled')
        if json.loads(credential['owner_json'])['kind'] not in {'human', 'route'} or parent['source'] == 'continuation':
            blockers.append('route_origin_unsupported')
        from .task_workspaces import for_turn
        if for_turn(parent['id']):
            blockers.append('route_task_unsupported')
        if budget and budget['remaining_turns'] < 2:
            blockers.append('route_turns_exhausted')
        if budget and budget['depth'] >= bounds['max_depth']:
            blockers.append('route_depth_exhausted')
        if parent['status'] != 'pending':
            blockers.append('parent_not_running')
        if conn.execute('SELECT 1 FROM route_requests WHERE parent_id=? UNION ALL SELECT 1 FROM continuations WHERE parent_id=?', (parent['id'], parent['id'])).fetchone():
            blockers.append('successor_already_declared')
        return dict(version=1, topic=parent['topic'], agent=parent['agent'], operations=catalog(),
                    scope=json.loads(credential['scope_json']), continuation_enabled=c._enabled,
                    routes=dict(supported=True, enabled=routes.settings()['enabled'], executable=not blockers,
                                blockers=blockers, limits=bounds, budget=budget,
                                subset='same_topic_immediate', execution_scope='local_work',
                                completion_gate='successful_completion_and_sync'))
