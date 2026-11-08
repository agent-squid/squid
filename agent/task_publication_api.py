"""Authenticated human publication controls; never exposed to turn credentials."""
import hashlib
import secrets
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import task_publication as publication, stats_db as db, task_workspaces as tasks
from .watch_api import bearer, fields
from .watch_async import call
from .watch_service import WatchDenied

router = APIRouter()


def human(request):
    token = bearer(request)
    expected = getattr(request.app.state, 'watch_control_digest', None)
    if expected is None or not secrets.compare_digest(hashlib.sha256(token.encode()).digest(), expected):
        raise WatchDenied()
    return 'host-control:' + expected.hex()[:16]


@router.post('/task-workspaces/{task_id}/publication-grant')
async def grant(request: Request, task_id: str):
    try:
        audit = human(request)
        body = fields(await request.json(), ('request_id','publication','expires_at'))
        await call(publication.record_grant, task_id, audit=audit, **body)
        return {'recorded': True, 'task_id': task_id}
    except (PermissionError, WatchDenied):
        return JSONResponse({'error':'publication_not_authorized'}, status_code=403)
    except (ValueError, TypeError, RuntimeError):
        return JSONResponse({'error':'invalid_publication_grant'}, status_code=400)


@router.post('/task-workspaces/{task_id}/publication-revoke')
async def revoke(request: Request, task_id: str):
    try:
        human(request)
        body = fields(await request.json(), ('request_id',))
        matched = await call(_revoke, task_id, body['request_id'])
        if not matched:
            return JSONResponse({'error': 'publication_grant_not_found'}, status_code=404)
        return {'revoked':True}
    except (PermissionError, WatchDenied):
        return JSONResponse({'error':'publication_not_authorized'}, status_code=403)
    except (ValueError, TypeError):
        return JSONResponse({'error':'invalid_publication_request'}, status_code=400)


@router.get('/task-publications/{identifier}')
async def status(request: Request, identifier: str):
    try:
        human(request)
        row = await call(publication.get, identifier)
        return {k:row[k] for k in ('id','task_id','state','base_sha','intended_sha','pr_number','pr_url','error')}
    except (PermissionError, WatchDenied):
        return JSONResponse({'error':'publication_not_authorized'}, status_code=403)
    except RuntimeError:
        return JSONResponse({'error':'publication_not_found'}, status_code=404)


@router.post('/task-publications/{identifier}/reconcile')
async def reconcile(request: Request, identifier: str):
    try:
        human(request)
        body = fields(await request.json(), optional=('authority_parent_id','resume'))
        if 'resume' in body and type(body['resume']) is not bool:
            raise ValueError()
        row = await call(_reconcile, identifier, body.get('authority_parent_id'), body.get('resume', False))
        await call(publication.finish, row)
        return {'state':row['state'], 'commit':row['intended_sha'], 'pr':row['pr_url']}
    except (PermissionError, WatchDenied):
        return JSONResponse({'error':'publication_not_authorized'}, status_code=403)
    except (ValueError, TypeError, RuntimeError):
        return JSONResponse({'error':'publication_reconciliation_blocked'}, status_code=409)
    except (OSError, subprocess.SubprocessError):
        return JSONResponse({'error':'publication_transport_unavailable'}, status_code=503)


def _reconcile(identifier, authority_parent_id, resume):
    import json
    from pathlib import Path
    from .worktree import _lock_for
    record = publication.get(identifier)
    path = Path(json.loads(record['destination_json'])['worktree_path'])
    with _lock_for(path):
        adopted = None
        if authority_parent_id is not None:
            from .continuation_owners import capture
            record = publication.get(identifier)
            task = tasks.for_turn(authority_parent_id)
            with db._connection() as conn:
                owner = capture(conn, authority_parent_id)
                if not owner or owner['kind'] != 'human' or not task or task['id'] != record['task_id']:
                    raise PermissionError()
                scope = publication.scope_for(conn, publication.encode(owner), task)
                if publication.encode(scope) != record['scope_json']:
                    raise PermissionError()
                import time
                adopted = conn.execute('INSERT INTO task_publication_authorities(publication_id,owner_json,created) VALUES(?,?,?)',
                             (identifier,publication.encode(owner),time.time())).lastrowid
        try:
            return publication._advance_locked(identifier, allow_effects=resume)
        except Exception:
            if adopted is not None:
                with db._connection() as conn:
                    conn.execute('DELETE FROM task_publication_authorities WHERE id=?', (adopted,))
            raise


def _revoke(task_id, request_id):
    with db._connection() as conn:
        return conn.execute('UPDATE task_publication_grants SET revoked=1 WHERE task_id=? AND request_id=?',
                            (task_id, request_id)).rowcount > 0
