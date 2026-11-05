"""Turn-scoped continuation transport; independent of watch enablement."""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import continuations as store
from .watch_api import bearer
from .watch_service import WatchDenied

router = APIRouter()


async def invoke(request, action, identifier=None):
    try:
        token = bearer(request)
        if action == 'create':
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 65536:
                    raise ValueError('invalid_continuation_request')
                raw.extend(chunk)
            return await asyncio.to_thread(store.create, token, json.loads(raw))
        return await asyncio.to_thread(store.inspect, token, identifier, action == 'cancel')
    except (PermissionError, WatchDenied):
        return JSONResponse({'error': 'continuation_not_authorized'}, status_code=403)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        errors = {'continuation_conflict': 409, 'parent_not_running': 409,
                  'original_scope_unavailable': 409, 'continuation_not_found': 404}
        code = str(exc) if str(exc) in errors else 'invalid_continuation_request'
        return JSONResponse({'error': code}, status_code=errors.get(code, 400))


@router.post('/continuations')
async def create(request: Request):
    return await invoke(request, 'create')


@router.get('/continuations')
async def list_continuations(request: Request):
    return await invoke(request, 'list')


@router.get('/continuations/{identifier}')
async def show(request: Request, identifier: str):
    return await invoke(request, 'show', identifier)


@router.post('/continuations/{identifier}/cancel')
async def cancel(request: Request, identifier: str):
    return await invoke(request, 'cancel', identifier)
