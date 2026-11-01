"""Lifespan-owned local watch dispatch through the ordinary topic queue."""
import asyncio
import json
import logging

from . import watch_runs as store
from .watch_async import call
from . import watches

log = logging.getLogger(__name__)


class WatchDispatch:
    def __init__(self, service, dispatcher):
        self.service = service
        self.dispatcher = dispatcher
        self.tasks = {}
        self.probes = set()
        self.accepting = True

    async def run(self):
        while True:
            try:
                await self.recover()
                if len(self.tasks) < 4:
                    # Queue state belongs to the loop; take a snapshot before DB work.
                    busy = self.dispatcher.busy_topics()
                    run_id = await call(store.claim_batch, self.service, busy.__contains__)
                    if run_id:
                        task = asyncio.create_task(self.dispatch(run_id), name='squid-watch-run-' + run_id)
                        self.tasks[run_id] = task
                        def finished(task, key=run_id):
                            self.tasks.pop(key, None)
                            if not task.cancelled() and task.exception() is not None:
                                log.error('Watch dispatch task failed run_id=%s error_type=%s', key, type(task.exception()).__name__)
                        task.add_done_callback(finished)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error('Watch dispatch tick failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(.5)

    async def recover(self):
        def unfinished():
            conn = watches._connect()
            try:
                return [r[0] for r in conn.execute("SELECT r.id FROM watch_runs r LEFT JOIN chat_messages m ON m.id=r.assistant_msg_id WHERE r.state IN ('claimed','queued','running') OR m.status='pending'")]
            finally:
                conn.close()
        for run_id in await call(unfinished):
            if run_id in self.tasks:
                continue
            await call(store.settle, self.service, run_id, interrupted=True)
            await call(store.finish_dispatch, run_id)

    async def dispatch(self, run_id):
        from .server import _prepare_chat_turn
        from fastapi.responses import JSONResponse
        try:
            run, watch, grant, events = await call(store.details, run_id)
            prepared = await _prepare_chat_turn(message=store.prompt_for(run, watch, grant, events),
                topic=watch['topic'], agent=grant['agent'], source='watch', watch_run_id=run_id)
            if isinstance(prepared, JSONResponse):
                if json.loads(prepared.body).get('error') == 'watch_worktree_blocked':
                    await self.defer(run_id, 'worktree_blocked')
                else:
                    await self.reject(run_id, 'preparation_failed')
                return
            await call(store.set_state, run_id, 'queued')
            keys = ('topic', 'context_history', 'harness', 'provider', 'backend', 'model', 'agent',
                    'cwd', 'source_cwd', 'configured_cwd', 'response_timeout', 'resume_session_id',
                    'adhoc', 'lookback', 'code_roots', 'display_prompt', 'worktree_setup_elapsed_ms',
                    'worktree_isolated')
            kwargs = {key: prepared[key] for key in keys}
            out, _, _ = await self.dispatcher.dispatch(prompt=prepared['effective_message'],
                msg_id=prepared['asst_msg_id'], watch_run_id=run_id, **kwargs)
            queue_error = False
            while True:
                chunk = await out.get()
                if chunk is None:
                    break
                queue_error |= isinstance(chunk, dict) and '_error' in chunk
            await call(store.settle, self.service, run_id, interrupted=True, queue_error=queue_error and self.accepting)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Watch run failed run_id=%s error_type=%s', run_id, type(exc).__name__)
            await self.reject(run_id, 'dispatch_failed')

    async def reject(self, run_id, reason):
        await call(store.finish_dispatch, run_id, 'cancelled', reason, pause_watch=True)

    async def before_execute(self, run_id):
        run, watch, grant, events = await call(store.details, run_id)
        if not self.accepting:
            return None
        # Refresh from the source at execution time. This read does not advance
        # the polling cursor or acknowledge events outside this captured batch.
        adapter = self.service.configuration.adapters.get(watch['adapter'])
        if adapter is None:
            await self.reject(run_id, 'adapter_unavailable')
            return None
        if not await call(self.authorized, run_id):
            await self.reject(run_id, 'authorization_changed')
            return None
        try:
            timeout = getattr(getattr(adapter, 'probe', None), 'timeout', 20) + 2
            async def authorize_read():
                if not self.accepting or not await call(self.authorized, run_id):
                    raise PermissionError('watch authorization changed')
            poll = getattr(adapter, 'poll_authorized', None)
            operation = poll(watch, authorize_read) if poll is not None else adapter.poll(watch)
            probe = asyncio.create_task(operation)
            self.probes.add(probe)
            try:
                observation = await asyncio.wait_for(probe, timeout)
            finally:
                self.probes.discard(probe)
            if observation.closed:
                await call(store.complete_source, self.service, run_id)
                return None
            snapshot = json.dumps(dict(revision=observation.cursor, events=observation.events), allow_nan=False)
            if len(snapshot.encode()) > 65536:
                snapshot = snapshot.encode()[:60000].decode(errors='ignore') + '\n[Fresh observation truncated]'
        except asyncio.CancelledError:
            if not self.accepting:
                return None
            raise
        except Exception:
            await self.reject(run_id, 'source_refresh_failed')
            return None
        if not self.accepting or not await call(store.begin, self.service, run_id, start=False):
            latest, _, _, _ = await call(store.details, run_id)
            if latest['state'] == 'deferred':
                await self.defer(run_id)
                return None
            await self.reject(run_id, 'authorization_changed')
            return None
        return store.prompt_for(run, watch, grant, events, snapshot)

    def authorized(self, run_id):
        with self.service._lock:
            conn = watches._connect()
            try:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    run = conn.execute('SELECT * FROM watch_runs WHERE id=?', (run_id,)).fetchone()
                    if run is None or run['state'] not in {'queued', 'running'}:
                        return False
                    watch = dict(conn.execute('SELECT * FROM watches WHERE id=?', (run['watch_id'],)).fetchone())
                    grant = conn.execute('SELECT * FROM watch_execution_grants WHERE id=?', (run['grant_id'],)).fetchone()
                    return store.authorized(self.service, conn, watch, grant, now=self.service.clock())
            finally:
                conn.close()

    async def defer(self, run_id, reason='user_work_priority'):
        await call(store.finish_dispatch, run_id, 'deferred', reason, now=self.service.clock())

    async def stop(self):
        from .runners import kill_proc_by_msg_id
        self.accepting = False
        for probe in list(self.probes):
            probe.cancel()
        await asyncio.gather(*list(self.probes), return_exceptions=True)
        for run_id, task in list(self.tasks.items()):
            run, watch, _, _ = await call(store.details, run_id)
            await call(store.settle, self.service, run_id, interrupted=True)
            if run['assistant_msg_id']:
                self.dispatcher.drain_topic(watch['topic'], msg_id=run['assistant_msg_id'])
                kill_proc_by_msg_id(run['assistant_msg_id'])
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)


async def refresh_run(run_id):
    from .server import app
    runtime = app.state.watch_service.dispatch_runtime
    return await runtime.before_execute(run_id) if runtime is not None else None


async def defer_run(run_id):
    from .server import app
    runtime = app.state.watch_service.dispatch_runtime
    if runtime is not None:
        await runtime.defer(run_id)


async def confirm_run(run_id):
    from .server import app
    runtime = app.state.watch_service.dispatch_runtime
    if runtime is None or not runtime.accepting:
        return False
    if await call(store.begin, runtime.service, run_id):
        return True
    latest, _, _, _ = await call(store.details, run_id)
    if latest['state'] == 'deferred':
        await runtime.defer(run_id)
    else:
        await runtime.reject(run_id, 'authorization_changed')
    return False
