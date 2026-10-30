"""Internal durable observation loop. No agent dispatch or publication authority.

Adapters perform reads only. Creation/activation must eventually be reached
through the authorized service; this module deliberately exposes no transport.
"""
import asyncio
from dataclasses import dataclass
import json
import logging
import math
import time
from typing import Protocol
from uuid import uuid4

log = logging.getLogger(__name__)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS watches (
        id TEXT PRIMARY KEY, topic TEXT NOT NULL, adapter TEXT NOT NULL,
        target TEXT NOT NULL, request_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'proposed'
            CHECK(status IN ('proposed','active','paused','cancelled','expired','completed')),
        version INTEGER NOT NULL DEFAULT 0,
        poll_interval REAL NOT NULL CHECK(poll_interval >= 30),
        expires_at REAL NOT NULL, next_poll REAL NOT NULL,
        cursor TEXT, claim_token TEXT, lease_until REAL,
        failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, pause_reason TEXT,
        UNIQUE(topic, request_key)
    )""",
    """CREATE TABLE IF NOT EXISTS watch_operations (
        watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        operation_key TEXT NOT NULL, action TEXT NOT NULL,
        expected_version INTEGER NOT NULL, result TEXT NOT NULL,
        PRIMARY KEY(watch_id, operation_key)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_watches_expiry ON watches(status, expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_watches_due
       ON watches(status, next_poll, lease_until)""",
    """CREATE TABLE IF NOT EXISTS watch_events (
        watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
        event_key TEXT NOT NULL, payload TEXT NOT NULL,
        observed_at REAL NOT NULL,
        disposition TEXT NOT NULL DEFAULT 'pending' CHECK(disposition IN ('pending','handled','superseded')),
        PRIMARY KEY(watch_id, event_key)
    )""",
)


def _connect():
    from .stats_db import _connect as connect
    return connect()


DEFAULT_WATCH_TTL = 24 * 60 * 60


def create_watch(*, topic, adapter, target, request_key, poll_interval, now, expires_at=None):
    """Creation keys are permanent per topic; a new watch needs a new key."""
    if not all(isinstance(v, str) and v.strip() for v in (topic, adapter, target, request_key)):
        raise ValueError('watch identity fields must be nonempty strings')
    default_expiry = expires_at is None
    if default_expiry:
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError('watch requires finite times')
        expires_at = now + DEFAULT_WATCH_TTL
    if (not all(type(v) in (int, float) and math.isfinite(v)
                for v in (poll_interval, expires_at, now))
            or poll_interval < 30):
        raise ValueError('watch requires finite times, interval >= 30s and future expiry')
    conn = _connect()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM watches WHERE topic=? AND request_key=?',
                                    (topic, request_key)).fetchone()
            if existing is None:
                if expires_at <= now:
                    raise ValueError('watch requires future expiry')
                conn.execute("""INSERT INTO watches
                    (id,topic,adapter,target,request_key,poll_interval,expires_at,next_poll)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (uuid4().hex, topic, adapter, target, request_key, poll_interval, expires_at, now))
                existing = conn.execute('SELECT * FROM watches WHERE topic=? AND request_key=?',
                                        (topic, request_key)).fetchone()
            row = dict(existing)
            if default_expiry:
                expires_at = row['expires_at']  # Retries never extend the original window.
            if any(row[k] != v for k, v in dict(adapter=adapter, target=target,
                                               poll_interval=poll_interval, expires_at=expires_at).items()):
                raise ValueError('request key already used with different watch parameters')
            return row
    finally:
        conn.close()


def transition_watch(watch_id, *, topic, version, action, now, operation_key=None):
    """Replay a keyed operation's original result, never reapply a stale action.

    Replays return the recorded result, which may differ from current state.
    Unkeyed requests retain strict optimistic version checks.
    """
    allowed = {'activate': (('proposed',), 'active'), 'pause': (('active',), 'paused'),
               'resume': (('paused',), 'active'),
               'cancel': (('proposed', 'active', 'paused'), 'cancelled')}
    if action not in allowed:
        raise ValueError('unsupported watch action')
    if operation_key is not None and (not isinstance(operation_key, str)
                                     or not operation_key.strip() or len(operation_key) > 512):
        raise ValueError('invalid operation key')
    sources, target = allowed[action]
    conn = _connect()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM watches WHERE id=? AND topic=?', (watch_id, topic)).fetchone()
            if row is None:
                raise ValueError('watch not found')
            if operation_key is not None:
                previous = conn.execute(
                    'SELECT * FROM watch_operations WHERE watch_id=? AND operation_key=?',
                    (watch_id, operation_key)).fetchone()
                if previous is not None:
                    if previous['action'] != action or previous['expected_version'] != version:
                        raise ValueError('operation key already used with different parameters')
                    return json.loads(previous['result'])
            if row['version'] != version:
                raise ValueError('stale watch version')
            if action != 'cancel' and row['expires_at'] <= now:
                raise ValueError('watch transition unavailable')
            if row['status'] != target:
                if row['status'] not in sources:
                    raise ValueError('watch transition unavailable')
                reset = action in ('activate', 'resume')
                conn.execute("""UPDATE watches SET status=?,version=version+1,
                    claim_token=NULL,lease_until=NULL,next_poll=?,pause_reason=?,
                    failures=CASE WHEN ? THEN 0 ELSE failures END,
                    last_error=CASE WHEN ? THEN NULL ELSE last_error END
                    WHERE id=? AND version=?""",
                    (target, now, 'manual' if action == 'pause' else None,
                     reset, reset, watch_id, version))
            result = dict(conn.execute('SELECT * FROM watches WHERE id=?', (watch_id,)).fetchone())
            if operation_key is not None:
                conn.execute('INSERT INTO watch_operations VALUES (?,?,?,?,?)',
                             (watch_id, operation_key, action, version, json.dumps(result)))
            return result
    finally:
        conn.close()


def claim_due(*, now, lease_seconds, limit):
    conn = _connect()
    try:
        # Read-only fast path avoids taking the shared DB write lock while
        # idle. Recheck under the write transaction if any work is eligible.
        due = conn.execute("""SELECT 1 FROM watches WHERE status='active'
            AND next_poll<=? AND (lease_until IS NULL OR lease_until<=?) LIMIT 1""",
            (now, now)).fetchone()
        expired = conn.execute("""SELECT 1 FROM watches
            WHERE status IN ('proposed','active','paused') AND expires_at<=? LIMIT 1""",
            (now,)).fetchone()
        if due is None and expired is None:
            return []
        with conn:
            # Serialize selection with claims and lifecycle updates; never hold
            # this transaction over provider IO.
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("""UPDATE watches SET status='expired',version=version+1,
                claim_token=NULL,lease_until=NULL,pause_reason=NULL
                WHERE status IN ('proposed','active','paused') AND expires_at<=?""", (now,))
            rows = conn.execute("""SELECT * FROM watches WHERE status='active'
                AND next_poll<=? AND (lease_until IS NULL OR lease_until<=?)
                ORDER BY next_poll,id LIMIT ?""", (now, now, limit)).fetchall()
            claims = []
            for row in rows:
                token = uuid4().hex
                conn.execute('UPDATE watches SET claim_token=?,lease_until=? WHERE id=?',
                             (token, now + lease_seconds, row['id']))
                claims.append(dict(row) | {'claim_token': token})
            return claims
    finally:
        conn.close()


@dataclass(frozen=True)
class Observation:
    """None preserves the cursor; event keys identify immutable revisions.

    Adapters must include source revision/attempt (e.g. SHA + check ID + run
    attempt, or comment ID + edit revision). Changed payloads need new keys.
    """
    cursor: str | None
    events: tuple[tuple[str, dict], ...] = ()
    closed: bool = False


class Adapter(Protocol):
    async def poll(self, watch: dict) -> Observation: ...


def encode_event_payload(payload):
    """Use the persisted representation for every event size check."""
    encoded = json.dumps(payload, allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise ValueError('oversized watch event')
    return encoded


def finish_poll(watch, *, now, observation=None, error=None):
    # Bound data before opening the transaction. Failure leaves the old cursor
    # unchanged so retries cannot skip incompletely ingested observations.
    events = []
    if observation is not None:
        if len(observation.events) > 1000 or (observation.cursor is not None and
                (not isinstance(observation.cursor, str) or len(observation.cursor) > 4096)):
            raise ValueError('observation exceeds limits')
        for key, payload in observation.events:
            encoded = encode_event_payload(payload)
            if not isinstance(key, str) or not key or len(key) > 512:
                raise ValueError('invalid or oversized watch event')
            events.append((watch['id'], key, encoded, now))
    conn = _connect()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("""SELECT * FROM watches WHERE id=? AND status='active'
                AND version=? AND claim_token=? AND lease_until>? AND expires_at>?""",
                (watch['id'], watch['version'], watch['claim_token'], now, now)).fetchone()
            if row is None:
                return False
            failures = row['failures'] + 1 if error else 0
            delay = min(max(3600, row['poll_interval']),
                        row['poll_interval'] * 2 ** min(failures, 6))
            status = 'paused' if failures >= 5 else 'active'
            cursor = row['cursor']
            if observation is not None and not error:
                conn.executemany('INSERT OR IGNORE INTO watch_events(watch_id,event_key,payload,observed_at) VALUES (?,?,?,?)', events)
                if observation.cursor is not None:
                    cursor = observation.cursor
                if observation.closed:
                    status = 'completed'
            conn.execute("""UPDATE watches SET status=?,cursor=?,failures=?,last_error=?,
                next_poll=?,claim_token=NULL,lease_until=NULL,pause_reason=?,
                version=version+CASE WHEN status!=? THEN 1 ELSE 0 END WHERE id=?""",
                (status, cursor, failures, error, now + delay,
                 'poll_failures' if status == 'paused' else None, status, watch['id']))
            return True
    finally:
        conn.close()


class WatchLoop:
    def __init__(self, adapters: dict[str, Adapter], *, concurrency=4, timeout=30, clock=time.time):
        if concurrency < 1 or timeout <= 0:
            raise ValueError('invalid watch worker bounds')
        for adapter in adapters.values():
            validate_timeout = getattr(adapter, 'validate_loop_timeout', None)
            if validate_timeout is not None:
                validate_timeout(timeout)
        self.adapters = adapters
        self.concurrency = concurrency
        self.timeout = timeout
        self.clock = clock
        self._tick_lock = asyncio.Lock()

    async def _poll(self, watch):
        try:
            adapter = self.adapters.get(watch['adapter'])
            if adapter is None:
                raise ValueError('unsupported adapter')
            observation = await asyncio.wait_for(adapter.poll(watch), self.timeout)
            await asyncio.to_thread(finish_poll, watch, now=self.clock(), observation=observation)
        except asyncio.CancelledError:
            # Leave the lease in place: restart reclaims it after expiry.
            raise
        except Exception as exc:
            # Provider exceptions may carry credentials or raw response bodies.
            log.warning('Watch poll failed watch_id=%s error_type=%s',
                        watch['id'], type(exc).__name__)
            try:
                await asyncio.to_thread(finish_poll, watch, now=self.clock(),
                                        error=type(exc).__name__)
            except Exception as persistence_error:
                log.error('Watch failure persistence failed watch_id=%s error_type=%s',
                          watch['id'], type(persistence_error).__name__)
                raise

    async def tick(self):
        async with self._tick_lock:
            claims = await asyncio.to_thread(claim_due, now=self.clock(),
                                            lease_seconds=self.timeout + 30, limit=self.concurrency)
            # A persistence failure must not release the tick lock while
            # sibling polls are still running and consume worker capacity.
            results = await asyncio.gather(*(self._poll(watch) for watch in claims),
                                           return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return len(claims)

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error('Watch maintenance tick failed error_type=%s', type(exc).__name__)
            await asyncio.sleep(1)
