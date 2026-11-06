"""Observation service shared by HTTP, CLI, and scoped one-shot launches.

WatchControl is a trusted host API, never a model-callable tool. Its callers
must authenticate the human and validate the structured action before recording
a grant. Message text/reference alone is not approval. API credentials constrain
calls here; they do not sandbox an agent's ambient filesystem/shell access.
"""
from dataclasses import dataclass
import asyncio
import hashlib
import json
import math
import secrets
import threading
import time
from uuid import uuid4

from . import watches
from .watch_policy import OPERATIONS


class WatchUnavailable(RuntimeError):
    pass


class WatchDenied(PermissionError):
    def __init__(self):
        super().__init__('watch operation not authorized')


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4096


def _time(value):
    return type(value) in (int, float) and math.isfinite(value)


def _public(row):
    result = {k: v for k, v in dict(row).items() if k not in {'claim_token', 'lease_until'}}
    from .watch_plans import PLAN_ERRORS, digest
    if result.get('observation_plan'):
        result['plan_digest'] = digest(json.loads(result['observation_plan']))
    if result.get('last_error') in PLAN_ERRORS:
        result['last_error_hint'] = PLAN_ERRORS[result['last_error']]
    return result


@dataclass(frozen=True, repr=False)
class _Credential:
    turn: str
    topic: str
    operations: frozenset[str]
    grants: frozenset[str]
    expires_at: float


class WatchService:
    def __init__(self, configuration, *, clock=time.time):
        self.configuration = configuration
        self.clock = clock
        # Opaque credentials are instance-local and disappear on restart.
        self._credentials = {}
        self._lock = threading.RLock()
        self._closed = False
        self._monitoring_task = None
        self._dispatch_task = None
        self.dispatch_runtime = None

    @property
    def dispatch_enabled(self):
        return (not self._closed and self.monitoring_enabled and self.configuration.policy.execution_enabled
                and self._dispatch_task is not None and not self._dispatch_task.done())

    def start_dispatch(self, dispatcher):
        if not self.monitoring_enabled or not self.configuration.policy.execution_enabled or self.dispatch_enabled:
            return
        from .watch_dispatch import WatchDispatch
        self.dispatch_runtime = WatchDispatch(self, dispatcher)
        self._dispatch_task = asyncio.create_task(self.dispatch_runtime.run(), name='squid-watch-dispatch')

    async def stop_dispatch(self):
        if self.dispatch_runtime is not None:
            self.dispatch_runtime.accepting = False
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            await asyncio.gather(self._dispatch_task, return_exceptions=True)
            self._dispatch_task = None
        if self.dispatch_runtime is not None:
            await self.dispatch_runtime.stop()
            self.dispatch_runtime = None

    @property
    def monitoring_enabled(self):
        task = self._monitoring_task
        return not self._closed and self.configuration.enabled and task is not None and not task.done()

    def start_monitoring(self):
        """Host-only lifecycle hook; polling grants no agent-dispatch authority."""
        if self._closed or not self.configuration.enabled or self.monitoring_enabled:
            return
        from .watch_probes import CLEANUP_TIMEOUT
        timeout = max([30] + [adapter.probe.timeout + CLEANUP_TIMEOUT + 1
                              for adapter in self.configuration.adapters.values()
                              if hasattr(adapter, 'probe')])
        loop = watches.WatchLoop(self.configuration.adapters, timeout=timeout, clock=self.clock,
                                 claim=self.claim_due, finish=self.finish_poll, prepare=self.prepare_poll)
        self._monitoring_task = asyncio.create_task(loop.run(), name='squid-watch-observation')

    async def stop_monitoring(self):
        task, self._monitoring_task = self._monitoring_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _poll_guard(self, conn, row, now, *, claimed=False):
        # A worker may have waited for SQLite or the service lock. Authorization
        # uses the current clock inside the write transaction, not its enqueue time.
        now = max(now, self.clock())
        if row['expires_at'] <= now:
            conn.execute('''UPDATE watches SET status='expired',version=version+1,
                claim_token=NULL,lease_until=NULL,pause_reason=NULL WHERE id=? AND status='active' ''',
                (row['id'],))
            conn.execute("UPDATE watch_events SET disposition='superseded' WHERE watch_id=? AND disposition='pending'",
                         (row['id'],))
            return False
        if claimed and (row['lease_until'] is None or row['lease_until'] <= now):
            return False
        policy = self.configuration.effective_policy(row['topic'])
        reason = None
        if (self._closed or not policy.enabled or row['adapter'] not in policy.adapters
                or row['adapter'] not in self.configuration.adapters
                or row['poll_interval'] < policy.min_poll_seconds
                or row['expires_at'] - now > policy.max_lifetime_seconds):
            reason = 'policy_changed'
        elif conn.execute('''SELECT 1 FROM watch_grants WHERE watch_id=?
                AND revoked_at IS NULL AND expires_at>? AND expires_at>=? LIMIT 1''',
                (row['id'], now, row['expires_at'])).fetchone() is None:
            reason = 'grant_required'
        if reason is None:
            return True
        conn.execute('''UPDATE watches SET status='paused', version=version+1,
            claim_token=NULL, lease_until=NULL, pause_reason=? WHERE id=? AND status='active' ''',
            (reason, row['id']))
        return False

    def claim_due(self, **kwargs):
        with self._lock:
            return watches.claim_due(**kwargs, _guard=self._poll_guard)

    def finish_poll(self, watch, **kwargs):
        with self._lock:
            return watches.finish_poll(watch, **kwargs,
                                       max_idle_poll_seconds=self.configuration.effective_policy(watch['topic']).max_idle_poll_seconds,
                                       _guard=lambda conn, row, now: self._poll_guard(conn, row, now, claimed=True))

    def prepare_poll(self, watch, *, now):
        with self._lock:
            conn = watches._connect()
            try:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute('''SELECT * FROM watches WHERE id=? AND status='active'
                        AND version=? AND claim_token=? AND lease_until>? AND expires_at>?''',
                        (watch['id'], watch['version'], watch['claim_token'], now, now)).fetchone()
                    return row is not None and self._poll_guard(conn, dict(row), now, claimed=True)
            finally:
                conn.close()

    def close(self):
        with self._lock:
            self._closed = True
            self._credentials.clear()

    def _authorize(self, token, operation):
        if self._closed or not isinstance(token, str) or len(token) > 512:
            raise WatchDenied()
        credential = self._credentials.get(hashlib.sha256(token.encode()).digest())
        if credential is None or credential.expires_at <= self.clock():
            raise WatchDenied()
        policy = self.configuration.effective_policy(credential.topic)
        if not policy.enabled or operation not in credential.operations & policy.operations:
            raise WatchDenied()
        return credential, policy

    def check_operation(self, token, operation):
        """Authenticate even an unavailable transport operation without mutating."""
        with self._lock:
            self._authorize(token, operation)

    def discover(self, token):
        with self._lock:
            credential = None
            for operation in OPERATIONS:
                try:
                    credential, policy = self._authorize(token, operation)
                    break
                except WatchDenied:
                    pass
            if credential is None:
                raise WatchDenied()
            return dict(scope='observation', topic=credential.topic,
                        operations=sorted(credential.operations & policy.operations),
                        adapters=sorted(policy.adapters), observation_plans='plan' in policy.adapters,
                        observation_tools_endpoint='/watch-tools', plan_validation_endpoint='/watches/validate',
                        monitoring_enabled=self.monitoring_enabled,
                        dispatch_enabled=self.dispatch_enabled and policy.execution_enabled, publication_enabled=False,
                        min_poll_seconds=policy.min_poll_seconds,
                        max_lifetime_seconds=policy.max_lifetime_seconds,
                        max_runs=policy.max_runs, max_attempts=policy.max_attempts,
                        execution_scope='local_work' if policy.execution_enabled else None)

    def _plan_adapter(self, token):
        from .watch_plans import PlanError
        with self._lock:
            _, policy = self._authorize(token, 'create')
            adapter = self.configuration.adapters.get('plan')
            if adapter is None:
                raise PlanError('plan_connection_required')
            if 'plan' not in policy.adapters:
                raise WatchDenied()
            return adapter

    async def discover_observation_tools(self, token):
        from .watch_async import call
        adapter = await call(self._plan_adapter, token)
        async def authorize():
            await call(self._plan_adapter, token)
        result = await asyncio.wait_for(adapter.discover(authorize=authorize), 20)
        await authorize()
        return result

    async def validate_plan(self, token, *, target, plan):
        from .watch_plans import digest
        from .watch_async import call
        adapter = await call(self._plan_adapter, token)
        if not _text(target):
            raise ValueError('invalid watch target')
        plan = adapter.check(plan)
        async def authorize():
            await call(self._plan_adapter, token)
        observation, schemas = await asyncio.wait_for(adapter.evaluate(plan, authorize=authorize), 20)
        await authorize()
        saved = dict(plan=plan, schemas=schemas, target=target, connections=adapter.connection_digests(plan))
        return dict(plan_digest=digest(saved), validated=saved,
                    preview=dict(candidate_events=len(observation.events), closed=observation.closed,
                                 baseline='first active poll (not this preview)' if plan['initial'] == 'quiet' else 'actionable',
                                 completeness='bounded snapshot; intermediate changes may be missed'))

    def _existing_plan(self, token, *, target, plan, request_key, poll_interval, expires_at):
        from .watch_plans import validate, canonical
        plan = validate(plan)
        with self._lock:
            self._plan_adapter(token)
            credential, _ = self._authorize(token, 'create')
            if not _text(target) or not _text(request_key) or not _time(poll_interval):
                raise ValueError('invalid watch identity')
            conn = watches._connect()
            try:
                row = conn.execute('SELECT * FROM watches WHERE topic=? AND request_key=?',
                                   (credential.topic, request_key)).fetchone()
                if row is None:
                    return None
                saved = json.loads(row['observation_plan']) if row['observation_plan'] else None
                if (row['adapter'] != 'plan' or saved is None or row['target'] != target
                        or canonical(saved['plan']) != canonical(plan)
                        or row['poll_interval'] != poll_interval
                        or (expires_at is not None and row['expires_at'] != expires_at)):
                    raise ValueError('request key already used with different watch parameters')
                return _public(row)
            finally:
                conn.close()

    async def create_plan(self, token, *, target, plan, request_key, poll_interval=30, expires_at=None):
        from .watch_plans import canonical
        from .watch_async import call
        existing = await call(self._existing_plan, token, target=target, plan=plan,
                              request_key=request_key, poll_interval=poll_interval, expires_at=expires_at)
        if existing is not None:
            return existing
        validated = await self.validate_plan(token, target=target, plan=plan)
        # No caller can supply validation evidence through the public create API.
        return await call(self.create, token, adapter='plan', target=target,
                          request_key=request_key, poll_interval=poll_interval, expires_at=expires_at,
                          _observation_plan=canonical(validated['validated']))

    def create(self, token, *, adapter, target, request_key, poll_interval=30, expires_at=None,
               _observation_plan=None):
        with self._lock:
            credential, policy = self._authorize(token, 'create')
            if adapter == 'plan' and _observation_plan is None:
                raise ValueError('observation plan validation required')
            if adapter not in policy.adapters:
                raise WatchDenied()
            if not _text(target) or not _text(request_key):
                raise ValueError('invalid watch identity')
            if not _time(poll_interval) or poll_interval < policy.min_poll_seconds:
                raise ValueError('watch poll interval below policy minimum')
            now = self.clock()
            if expires_at is not None and not _time(expires_at):
                raise ValueError('watch expiry outside policy')
            # Store's policy-bounded default preserves expiry on idempotent retry.
            def guard(conn):
                self._authorize(token, 'create')
                existing = conn.execute(
                    'SELECT expires_at FROM watches WHERE topic=? AND request_key=?',
                    (credential.topic, request_key)).fetchone()
                if existing is None:
                    if expires_at is not None and not self.clock() < expires_at <= self.clock() + policy.max_lifetime_seconds:
                        raise ValueError('watch expiry outside policy')
            return _public(watches.create_watch(
                topic=credential.topic, adapter=adapter, target=target, request_key=request_key,
                poll_interval=poll_interval, expires_at=expires_at, now=now, _guard=guard,
                observation_plan=_observation_plan,
                _default_ttl=min(watches.DEFAULT_WATCH_TTL, policy.max_lifetime_seconds)))

    def show(self, token, watch_id):
        with self._lock:
            credential, _ = self._authorize(token, 'show')
            conn = watches._connect()
            try:
                row = conn.execute('SELECT * FROM watches WHERE id=? AND topic=?',
                                   (watch_id, credential.topic)).fetchone()
                if row is None:
                    raise WatchDenied()
                return _public(row)
            finally:
                conn.close()

    def list(self, token, *, limit=100, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError('invalid watch pagination')
        with self._lock:
            credential, _ = self._authorize(token, 'list')
            conn = watches._connect()
            try:
                return [_public(row) for row in conn.execute(
                    'SELECT * FROM watches WHERE topic=? ORDER BY id LIMIT ? OFFSET ?',
                    (credential.topic, limit, offset))]
            finally:
                conn.close()

    def runs(self, token, watch_id, *, limit=20, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ValueError('invalid run pagination')
        with self._lock:
            self.show(token, watch_id)
            conn = watches._connect()
            try:
                return [dict(row) for row in conn.execute(
                    'SELECT * FROM watch_runs WHERE watch_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                    (watch_id, limit, offset))]
            finally:
                conn.close()

    def transition(self, token, watch_id, *, version, action, grant_id=None, operation_key=None,
                   require_monitoring=False):
        if grant_id is not None and not _text(grant_id):
            raise ValueError('invalid grant identity')
        if action not in OPERATIONS - {'create', 'list', 'show'}:
            raise ValueError('unsupported watch action')
        if type(version) is not int or version < 0:
            raise ValueError('invalid watch version')
        with self._lock:
            credential, _ = self._authorize(token, action)

            def guard(conn, row):
                credential, policy = self._authorize(token, action)
                if action in {'activate', 'resume'}:
                    if require_monitoring and not self.monitoring_enabled:
                        raise WatchUnavailable()
                    if (row['expires_at'] <= self.clock() or row['adapter'] not in policy.adapters
                            or row['poll_interval'] < policy.min_poll_seconds):
                        raise WatchDenied()
                    if row['expires_at'] - self.clock() > policy.max_lifetime_seconds:
                        raise WatchDenied()
                    if grant_id not in credential.grants:
                        raise WatchDenied()
                    grant = conn.execute('''SELECT 1 FROM watch_grants WHERE id=?
                        AND watch_id=? AND revoked_at IS NULL AND expires_at>? AND expires_at>=?''',
                        (grant_id, row['id'], self.clock(), row['expires_at'])).fetchone()
                    if grant is None:
                        raise WatchDenied()

            return _public(watches.transition_watch(
                watch_id, topic=credential.topic, version=version, action=action,
                now=self.clock(), operation_key=operation_key, _guard=guard))


class WatchControl:
    """Trusted host-only entry point; never expose these methods as agent tools."""
    def __init__(self, service):
        self.service = service

    def mint(self, *, turn, topic, operations, expires_at, grants=()):
        # Launch code must revoke_turn in all terminal paths before integration.
        if not _text(turn) or not _text(topic):
            raise ValueError('invalid turn identity')
        if (not isinstance(operations, (list, tuple, set, frozenset)) or
                any(not isinstance(op, str) for op in operations) or
                not set(operations) <= OPERATIONS or
                not isinstance(grants, (list, tuple, set, frozenset)) or
                any(not _text(grant) for grant in grants)):
            raise ValueError('invalid credential scope')
        service = self.service
        with service._lock:
            now = service.clock()
            if not _time(expires_at) or not now < expires_at <= now + 3600:
                raise ValueError('turn credential lifetime must be at most one hour')
            policy = service.configuration.effective_policy(topic)
            if service._closed or not policy.enabled or not set(operations) <= policy.operations:
                raise WatchDenied()
            service._credentials = {k: v for k, v in service._credentials.items() if v.expires_at > now}
            token = secrets.token_urlsafe(32)
            service._credentials[hashlib.sha256(token.encode()).digest()] = _Credential(
                turn, topic, frozenset(operations), frozenset(grants), expires_at)
            return token

    def revoke_turn(self, turn):
        with self.service._lock:
            self.service._credentials = {
                k: v for k, v in self.service._credentials.items() if v.turn != turn}

    def record_grant(self, *, topic, watch_id, user_action, expires_at):
        """Record an already-authenticated structured human observation grant.

        user_action is an audit reference supplied by the trusted control path,
        never a claim extracted from model text. Only this exact watch is covered.
        """
        service = self.service
        with service._lock:
            now = service.clock()
            if not _text(user_action) or not _time(expires_at) or expires_at <= now:
                raise ValueError('invalid watch grant')
            policy = service.configuration.effective_policy(topic)
            conn = watches._connect()
            try:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute('SELECT * FROM watches WHERE id=? AND topic=?',
                                       (watch_id, topic)).fetchone()
                    if (service._closed or row is None or not policy.enabled or row['adapter'] not in policy.adapters
                            or row['status'] not in {'proposed', 'active', 'paused'}
                            or not now < row['expires_at'] <= expires_at
                            or expires_at > now + policy.max_lifetime_seconds
                            or row['poll_interval'] < policy.min_poll_seconds):
                        raise WatchDenied()
                    grant_id = uuid4().hex
                    conn.execute('INSERT INTO watch_grants VALUES (?,?,?,?,?,NULL)',
                                 (grant_id, watch_id, user_action, now, expires_at))
                    return grant_id
            finally:
                conn.close()

    def revoke_grant(self, *, topic, grant_id, watch_id=None):
        if not _text(grant_id) or (watch_id is not None and not _text(watch_id)):
            raise ValueError('invalid grant identity')
        service = self.service
        with service._lock:
            conn = watches._connect()
            try:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    grant = conn.execute('''SELECT g.* FROM watch_grants g JOIN watches w
                        ON w.id=g.watch_id WHERE g.id=? AND w.topic=?''', (grant_id, topic)).fetchone()
                    if grant is None or (watch_id is not None and grant['watch_id'] != watch_id):
                        raise WatchDenied()
                    conn.execute('UPDATE watch_grants SET revoked_at=COALESCE(revoked_at,?) WHERE id=?',
                                 (service.clock(), grant_id))
                    # Keep observation active while another full-lifetime grant
                    # still covers this exact watch. Otherwise invalidate leases.
                    conn.execute('''UPDATE watches SET status='paused',version=version+1,
                        claim_token=NULL,lease_until=NULL,pause_reason='grant_revoked'
                        WHERE id=? AND status='active' AND NOT EXISTS (
                            SELECT 1 FROM watch_grants g WHERE g.watch_id=watches.id
                            AND g.revoked_at IS NULL AND g.expires_at>?
                            AND g.expires_at>=watches.expires_at)
                        ''', (grant['watch_id'], service.clock()))
            finally:
                conn.close()
