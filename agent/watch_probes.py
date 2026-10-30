"""Read-only probe adapters. Registrations are trusted server configuration.

MCP transport/authentication belongs to the host: inject its call_tool client.
Neither transport executes model-supplied commands or discovers credentials.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
import signal
from typing import Awaitable, Callable

from .watches import Observation, encode_event_payload

CLEANUP_TIMEOUT = 1


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class Probe:
    """A registered read operation; arguments must include target/cursor fields.

    Executables receive one JSON request on stdin and emit one JSON result.
    MCP tools receive the same object as structured arguments. Credentials stay
    in the configured executable environment or the injected MCP client.
    """
    argv: tuple[str, ...] = ()
    server: str | None = None
    tool: str | None = None
    timeout: float = 20
    output_limit: int = 65536

    def __post_init__(self):
        local = bool(self.argv)
        remote = bool(self.server and self.tool)
        if local == remote or (local and (self.server or self.tool)):
            raise ValueError('register exactly one executable or MCP tool')
        if (not isinstance(self.argv, tuple)
                or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in self.argv)
                or (remote and any(not isinstance(v, str) or not v.strip()
                                   for v in (self.server, self.tool)))
                or type(self.timeout) not in (int, float)
                or not math.isfinite(self.timeout) or self.timeout <= 0
                or type(self.output_limit) is not int
                or not 1 <= self.output_limit <= 1048576):
            raise ValueError('invalid probe registration')


def normalize(result, *, cursor, output_limit=65536):
    """Normalize a revision snapshot; first result establishes a quiet baseline.

    A wrapper maps provider output to revision/changed/summary/url/closed. changed
    defaults to revision comparison; explicit true includes actionable initial
    state, false records a baseline without work. Revisions must identify immutable
    snapshots (including edits/attempts), not recurring status labels or content
    hashes. For CI use SHA + run ID + attempt; for comments use ID + edit version.
    Re-observing an old source version is a replay, not a new occurrence.
    No revision change means no new event.
    """
    if not isinstance(result, dict) or len(_json(result).encode()) > output_limit:
        raise ValueError('invalid or oversized probe result')
    if set(result) - {'revision', 'changed', 'summary', 'url', 'closed'}:
        raise ValueError('unknown probe result field')
    revision = result.get('revision')
    if not isinstance(revision, str) or not revision or len(revision) > 4096:
        raise ValueError('probe requires a bounded revision')
    for field in ('changed', 'closed'):
        if field in result and type(result[field]) is not bool:
            raise ValueError('probe flags must be booleans')
    for field in ('summary', 'url'):
        if field in result and not isinstance(result[field], str):
            raise ValueError('probe text must be strings')
    changed = result.get('changed', cursor is not None and revision != cursor)
    events = ()
    if changed and revision != cursor:
        encode_event_payload(result)
        key = 'probe:' + hashlib.sha256(revision.encode()).hexdigest()
        events = ((key, result),)
    return Observation(revision, events, result.get('closed', False))


class ProbeAdapter:
    def __init__(self, probe: Probe, *, call_tool: Callable[..., Awaitable[dict]] | None = None):
        if probe.server and call_tool is None:
            raise ValueError('MCP probe requires a server-owned client')
        self.probe = probe
        self.call_tool = call_tool

    def validate_loop_timeout(self, timeout):
        if self.probe.timeout + CLEANUP_TIMEOUT >= timeout:
            raise ValueError('watch timeout must exceed probe timeout plus cleanup allowance')

    async def _execute(self, request):
        process = await asyncio.create_subprocess_exec(
            *self.probe.argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=self.probe.output_limit + 1, start_new_session=True)
        async def exchange():
            async def write():
                try:
                    process.stdin.write((_json(request) + '\n').encode())
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()
            async def read():
                chunks, size = [], 0
                while chunk := await process.stdout.read(8192):
                    size += len(chunk)
                    if size > self.probe.output_limit:
                        raise ValueError('probe output exceeds limit')
                    chunks.append(chunk)
                return b''.join(chunks)
            tasks = [asyncio.create_task(write()), asyncio.create_task(read())]
            try:
                _, output = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            if await process.wait():
                raise RuntimeError('probe executable failed')
            return json.loads(output)
        try:
            return await exchange()
        finally:
            # Kill the group even if the leader already exited: descendants can
            # still hold stdout open. A new session isolates our own process.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            async def drain():
                while await process.stdout.read(8192):
                    pass
                await process.wait()
            try:
                await asyncio.wait_for(drain(), CLEANUP_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                # A descendant may have detached from the group. asyncio has
                # no public close-pipes API; close the transport rather than
                # waiting indefinitely for that unrelated process to reach EOF.
                process._transport.close()
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise

    async def poll(self, watch):
        request = {'target': watch['target'], 'cursor': watch['cursor']}
        if self.probe.argv:
            operation = self._execute(request)
        else:
            # Client unwraps MCP structuredContent and raises on isError.
            operation = self.call_tool(self.probe.server, self.probe.tool, request)
        result = await asyncio.wait_for(operation, self.probe.timeout)
        return normalize(result, cursor=watch['cursor'], output_limit=self.probe.output_limit)
