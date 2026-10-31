"""Observation-only watch transport. Human controls use a separate host secret."""
import hashlib
import ipaddress
import json
import secrets
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .watch_async import call
from .watch_service import WatchControl, WatchDenied

router = APIRouter()


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
    if isinstance(exc, WatchDenied):
        return JSONResponse({'error': 'watch_not_authorized'}, status_code=403)
    # Never return arbitrary exception/provider text or request payloads.
    conflict = isinstance(exc, ValueError) and str(exc) in {
        'stale watch version', 'request key already used with different watch parameters',
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
        if operation == 'tools':
            return await call(service.discover, token)
        if operation == 'list':
            fields(dict(request.query_params), optional=('limit', 'offset'))
            return await call(service.list, token, limit=int(request.query_params.get('limit', 100)),
                                offset=int(request.query_params.get('offset', 0)))
        if operation == 'show':
            return await call(service.show, token, watch_id)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 65536:
                raise ValueError('watch request too large')
            raw.extend(chunk)
        body = json.loads(raw)
        if operation == 'create':
            fields(body, ('adapter', 'target', 'request_key'), ('poll_interval', 'expires_at'))
            return await call(service.create, token, **body)
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
            # The observation engine is not started by lifespan yet. A saved
            # registration must never be presented as a working monitor.
            return JSONResponse({'error': 'watch_monitoring_unavailable'}, status_code=409)
        return await call(service.transition, token, watch_id, action=operation, **body)
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


@router.get('/watch-control/{topic}/watches/{watch_id}')
async def human_show(request: Request, topic: str, watch_id: str):
    return await invoke(request, 'show', watch_id, human_topic=topic)


@router.post('/watch-control/{topic}/watches/{watch_id}/{action}')
async def human_action(request: Request, topic: str, watch_id: str, action: str):
    if action not in {'activate', 'pause', 'resume', 'cancel', 'grant', 'revoke'}:
        return JSONResponse({'error': 'invalid_watch_action'}, status_code=400)
    return await invoke(request, action, watch_id, human_topic=topic)
