"""Turn credential routes; no implicit operator or persistent harness setup."""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import routes, shared_tools
from .watch_api import bearer

router = APIRouter()
ERRORS = {'route_conflict': 409, 'parent_not_running': 409, 'route_not_found': 404,
          'unsupported_route': 400, 'route_depth_exhausted': 409,
          'route_turns_exhausted': 409, 'invalid_route_request': 400}


async def invoke(request, action, identifier=None):
    try:
        token = bearer(request)
        if action == 'capabilities':
            return await asyncio.to_thread(shared_tools.discover, token)
        if action in {'validate', 'launch'}:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 65536:
                    raise ValueError('invalid_route_request')
                raw.extend(chunk)
            return await asyncio.to_thread(getattr(routes, action), token, json.loads(raw))
        result = await asyncio.to_thread(routes.inspect, token, identifier, action == 'cancel')
        if action == 'cancel':
            from .server import dispatcher
            await routes.maintain_stops(dispatcher)
        return result
    except PermissionError:
        return JSONResponse(dict(error='route_not_authorized'), status_code=403)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        code = str(exc) if str(exc) in ERRORS else 'invalid_route_request'
        return JSONResponse(dict(error=code), status_code=ERRORS.get(code, 400))


@router.get('/capabilities')
async def capabilities(request: Request):
    return await invoke(request, 'capabilities')


@router.post('/routes/validate')
async def validate(request: Request):
    return await invoke(request, 'validate')


@router.post('/routes')
async def launch(request: Request):
    return await invoke(request, 'launch')


@router.get('/routes')
async def list_routes(request: Request):
    return await invoke(request, 'list')


@router.get('/routes/{identifier}')
async def show(request: Request, identifier: str):
    return await invoke(request, 'show', identifier)


@router.post('/routes/{identifier}/cancel')
async def cancel(request: Request, identifier: str):
    return await invoke(request, 'cancel', identifier)


@router.post('/agent-history/{action}')
async def history(request: Request, action: str):
    from . import history_tools
    try:
        raw = bytearray()
        async for chunk in request.stream():
            # 512 non-BMP characters can occupy 6,144 bytes as JSON escapes.
            if len(raw) + len(chunk) > 8192:
                raise ValueError('invalid_history_request')
            raw.extend(chunk)
        return await asyncio.to_thread(history_tools.invoke, bearer(request), action, json.loads(raw))
    except PermissionError:
        return JSONResponse(dict(error='route_not_authorized'), status_code=403)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        code = 'history_not_found' if str(exc) == 'history_not_found' else 'invalid_history_request'
        return JSONResponse(dict(error=code), status_code=404 if code == 'history_not_found' else 400)
