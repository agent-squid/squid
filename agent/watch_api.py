"""Observation-only watch transport. Human controls use a separate host secret."""
import asyncio
import hashlib
import ipaddress
import json
import secrets
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .watch_mcp import MCPProbeError
from .watch_async import call
from .watch_service import WatchControl, WatchDenied, WatchUnavailable

router = APIRouter()


@router.get('/watch-status')
async def watch_status(request: Request):
    """Compact browser status; never expose grants, credentials or source payloads."""
    from .server import _same_origin
    if not _same_origin(request):
        return JSONResponse({'error': 'forbidden'}, status_code=403)
    service = request.app.state.watch_service
    def snapshot():
        from .watches import _connect
        now = service.clock()
        with _connect() as conn:
            rows = conn.execute('''SELECT w.id,w.topic,w.target,w.status,w.version,
                w.next_poll,w.expires_at,w.lease_until,w.last_error,w.pause_reason,
                g.agent,g.flow_route,g.instructions
                FROM watches w LEFT JOIN watch_execution_grants g
                ON g.watch_id=w.id AND g.revoked_at IS NULL
                WHERE w.status IN ('active','paused') AND w.expires_at>?
                ORDER BY w.topic,w.id''', (now,)).fetchall()
        result = []
        for row in rows:
            item = {k: row[k] for k in ('id','topic','target','status','version','next_poll',
                                       'agent','flow_route','instructions')}
            item['polling'] = row['lease_until'] is not None and row['lease_until'] > now
            item['error'] = bool(row['last_error'])
            item['monitoring'] = service.monitoring_enabled
            result.append(item)
        return result
    return await call(snapshot)


@router.post('/watch-status/{watch_id}/cancel')
async def cancel_status_watch(request: Request, watch_id: str):
    # Same host-control boundary as browser configuration/stop controls. This
    # endpoint can only cancel; it cannot create grants or activate monitoring.
    from .server import _same_origin
    if not _same_origin(request):
        return JSONResponse({'error': 'forbidden'}, status_code=403)
    try:
        body = fields(await request.json(), ('version',))
        if type(body['version']) is not int or body['version'] < 0:
            raise ValueError('invalid version')
        service = request.app.state.watch_service
        def cancel():
            from .watches import _connect, transition_watch
            with _connect() as conn:
                row = conn.execute('SELECT topic FROM watches WHERE id=?', (watch_id,)).fetchone()
            if row is None:
                raise ValueError('watch not found')
            transition_watch(watch_id, topic=row['topic'], version=body['version'],
                             action='cancel', now=service.clock())
        await call(cancel)
        return {'cancelled': True}
    except (ValueError, TypeError) as exc:
        return failure(exc)


def bearer(request):
    if request.url.scheme != 'https':
        try:
            if not ipaddress.ip_address(request.client.host).is_loopback:
                raise WatchDenied()
        except (ValueError, AttributeError):
            raise WatchDenied() from None
    value = request.headers.get('authorization', '')
    if not value.startswith('Bearer ') or len(value) > 520:
        raise WatchDenied()
    return value[7:]


def fields(body, required=(), optional=()):
    if not isinstance(body, dict) or set(body) - set(required) - set(optional) or set(required) - set(body):
        raise ValueError('invalid watch request fields')
    return body


def failure(exc):
    from .watch_plans import PlanError, PLAN_ERRORS
    if isinstance(exc, PlanError):
        return JSONResponse({'error': exc.code, 'hint': PLAN_ERRORS[exc.code]}, status_code=400)
    if isinstance(exc, WatchDenied):
        return JSONResponse({'error': 'watch_not_authorized'}, status_code=403)
    # Never return arbitrary exception/provider text or request payloads.
    conflict = isinstance(exc, ValueError) and str(exc) in {
        'stale watch version', 'execution grant request conflict', 'execution grant already exists', 'request key already used with different watch parameters',
        'operation key already used with different parameters', 'watch transition unavailable'}
    return JSONResponse({'error': 'watch_conflict' if conflict else 'invalid_watch_request'},
                        status_code=409 if conflict else 400)


async def invoke(request, operation, watch_id=None, *, human_topic=None):
    service = request.app.state.watch_service
    control = WatchControl(service)
    turn = None
    try:
        token = bearer(request)
        if human_topic is not None:
            expected = getattr(request.app.state, 'watch_control_digest', None)
            if expected is None or not secrets.compare_digest(hashlib.sha256(token.encode()).digest(), expected):
                raise WatchDenied()
            policy = service.configuration.effective_policy(human_topic)
            turn = 'human:' + uuid4().hex
            token = await call(control.mint, turn=turn, topic=human_topic, operations=policy.operations,
                                 expires_at=service.clock() + 60)
        if operation == 'observation-tools':
            return await service.discover_observation_tools(token)
        if operation == 'tools':
            return await call(service.discover, token)
        if operation == 'list':
            fields(dict(request.query_params), optional=('limit', 'offset'))
            return await call(service.list, token, limit=int(request.query_params.get('limit', 100)),
                                offset=int(request.query_params.get('offset', 0)))
        if operation == 'runs':
            fields(dict(request.query_params), optional=('limit', 'offset'))
            return await call(service.runs, token, watch_id, limit=int(request.query_params.get('limit', 20)),
                              offset=int(request.query_params.get('offset', 0)))
        if operation == 'show':
            return await call(service.show, token, watch_id)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 65536:
                raise ValueError('watch request too large')
            raw.extend(chunk)
        body = json.loads(raw)
        if operation == 'validate':
            fields(body, ('target', 'plan'))
            result = await service.validate_plan(token, **body)
            return {k: v for k, v in result.items() if k != 'validated'}
        if operation == 'create':
            if isinstance(body, dict) and 'plan' in body:
                fields(body, ('target', 'plan', 'request_key'), ('poll_interval', 'expires_at'))
                return await service.create_plan(token, **body)
            fields(body, ('adapter', 'target', 'request_key'), ('poll_interval', 'expires_at'))
            return await call(service.create, token, **body)
        if operation == 'execution-grant' and human_topic is not None:
            fields(body, ('instructions', 'max_runs', 'max_attempts', 'request_key'), ('agent', 'flow_route'))
            from .watch_runs import execution_grant
            audit = json.dumps(dict(id=turn, principal='host-control:' + expected.hex()[:16],
                                    action='grant_local_work', topic=human_topic, watch_id=watch_id))
            grant = await call(execution_grant, service, topic=human_topic, watch_id=watch_id,
                               user_action=audit, **body)
            return dict(grant_id=grant['id'], scope='local_work', watch_id=watch_id,
                        agent=grant['agent'] if not grant['flow_route'] else None, flow_route=grant['flow_route'], max_runs=grant['max_runs'], max_attempts=grant['max_attempts'],
                        revoked=grant['revoked_at'] is not None)
        if operation == 'execution-revoke' and human_topic is not None:
            fields(body, ('grant_id',))
            from .watch_runs import revoke_execution
            await call(revoke_execution, service, topic=human_topic, watch_id=watch_id, **body)
            return {'revoked': True}
        if operation == 'grant' and human_topic is not None:
            fields(body, ('expires_at',))
            audit = json.dumps(dict(id=turn, principal='host-control:' + expected.hex()[:16],
                                    action='grant_observation', topic=human_topic,
                                    watch_id=watch_id, expires_at=body['expires_at']))
            grant = await call(control.record_grant, topic=human_topic, watch_id=watch_id,
                               user_action=audit, expires_at=body['expires_at'])
            return {'grant_id': grant, 'scope': 'observation', 'watch_id': watch_id}
        if operation == 'revoke' and human_topic is not None:
            fields(body, ('grant_id',))
            # Verify both topic and watch binding before revoking.
            await call(service.show, token, watch_id)
            await call(control.revoke_grant, topic=human_topic, grant_id=body['grant_id'], watch_id=watch_id)
            return {'revoked': True}
        if operation not in {'activate', 'pause', 'resume', 'cancel'}:
            raise ValueError('unsupported watch operation')
        fields(body, ('version',), ('grant_id', 'operation_key'))
        if operation in {'activate', 'resume'}:
            await call(service.check_operation, token, operation)
            if not service.monitoring_enabled:
                return JSONResponse({'error': 'watch_monitoring_unavailable'}, status_code=409)
            if human_topic is not None:
                # The authenticated operator may attach an exact grant; model
                # arguments cannot expand an agent credential's grant scope.
                await call(control.revoke_turn, turn)
                token = await call(control.mint, turn=turn, topic=human_topic,
                                   operations=policy.operations, grants=[body.get('grant_id')],
                                   expires_at=service.clock() + 60)
        return await call(service.transition, token, watch_id, action=operation, require_monitoring=True, **body)
    except MCPProbeError:
        return JSONResponse({'error': 'plan_connection_unavailable'}, status_code=400)
    except asyncio.TimeoutError:
        return JSONResponse({'error': 'plan_timeout'}, status_code=400)
    except WatchUnavailable:
        return JSONResponse({'error': 'watch_monitoring_unavailable'}, status_code=409)
    except (WatchDenied, ValueError, TypeError, UnicodeError) as exc:
        return failure(exc)
    finally:
        if turn is not None:
            await call(control.revoke_turn, turn)


@router.get('/tools')
async def tools(request: Request):
    return await invoke(request, 'tools')


@router.get('/watches')
async def list_watches(request: Request):
    return await invoke(request, 'list')


@router.post('/watches')
async def create_watch(request: Request):
    return await invoke(request, 'create')


@router.get('/watch-tools')
async def observation_tools(request: Request):
    return await invoke(request, 'observation-tools')


@router.post('/watches/validate')
async def validate_plan(request: Request):
    return await invoke(request, 'validate')


@router.get('/watches/{watch_id}')
async def show_watch(request: Request, watch_id: str):
    return await invoke(request, 'show', watch_id)


@router.post('/watches/{watch_id}/{action}')
async def transition_watch(request: Request, watch_id: str, action: str):
    if action not in {'activate', 'pause', 'resume', 'cancel'}:
        return JSONResponse({'error': 'invalid_watch_action'}, status_code=400)
    return await invoke(request, action, watch_id)


@router.api_route('/watch-control/{topic}/watches', methods=['GET', 'POST'])
async def human_watches(request: Request, topic: str):
    return await invoke(request, 'list' if request.method == 'GET' else 'create', human_topic=topic)


@router.get('/watch-control/{topic}/tools')
async def human_tools(request: Request, topic: str):
    return await invoke(request, 'tools', human_topic=topic)


@router.get('/watch-control/{topic}/watch-tools')
async def human_observation_tools(request: Request, topic: str):
    return await invoke(request, 'observation-tools', human_topic=topic)


@router.post('/watch-control/{topic}/watches/validate')
async def human_validate_plan(request: Request, topic: str):
    return await invoke(request, 'validate', human_topic=topic)


@router.get('/watch-control/{topic}/watches/{watch_id}')
async def human_show(request: Request, topic: str, watch_id: str):
    return await invoke(request, 'show', watch_id, human_topic=topic)


@router.post('/watch-control/{topic}/watches/{watch_id}/{action}')
async def human_action(request: Request, topic: str, watch_id: str, action: str):
    if action not in {'activate', 'pause', 'resume', 'cancel', 'grant', 'revoke', 'execution-grant', 'execution-revoke'}:
        return JSONResponse({'error': 'invalid_watch_action'}, status_code=400)
    return await invoke(request, action, watch_id, human_topic=topic)


@router.get('/watches/{watch_id}/runs')
async def watch_runs(request: Request, watch_id: str, limit: int = 20, offset: int = 0):
    service = request.app.state.watch_service
    try:
        token = bearer(request)
        return await call(service.runs, token, watch_id, limit=limit, offset=offset)
    except (WatchDenied, ValueError, TypeError) as exc:
        return failure(exc)


@router.get('/watch-control/{topic}/watches/{watch_id}/runs')
async def human_watch_runs(request: Request, topic: str, watch_id: str):
    return await invoke(request, 'runs', watch_id, human_topic=topic)
