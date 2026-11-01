"""Trusted, explicit probe registry. Loading it never starts polling or a client.

Only host-owned configuration may reach this module. Registrations declare read
operations; tool discovery is not authority to invoke additional tools.
"""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
import json
import logging
import os
import re
from urllib.parse import urlsplit

from .watch_probes import Probe, ProbeAdapter


_probe_logging = ContextVar('watch_mcp_probe_logging', default=False)


def _install_log_redaction():
    # Redact before any handler (including SDK-owned handlers) sees the record.
    # Parent logger filters do not apply to records emitted by child loggers.
    # ContextVar scopes this to the probe and its transport tasks, so concurrent
    # users of these libraries keep their diagnostics. Preserve the host factory.
    factory = logging.getLogRecordFactory()
    if getattr(factory, '_watch_mcp_redaction', False):
        return

    def redact(*args, **kwargs):
        record = factory(*args, **kwargs)
        if _probe_logging.get() and record.name.split('.', 1)[0] in {'mcp', 'httpx', 'httpcore'}:
            record.msg = 'MCP probe transport diagnostic redacted'
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return record

    redact._watch_mcp_redaction = True
    logging.setLogRecordFactory(redact)


class MCPProbeError(RuntimeError):
    """A provider failure whose message contains no provider or credential data."""


class MCPCredentialError(MCPProbeError):
    """A missing or invalid configured credential; safe to report by type."""


def _mapping(value, fields):
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError('invalid probe configuration fields')
    return value


def _name(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,127}', value)


def _argv(value):
    if (not isinstance(value, list) or not value
            or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in value)):
        raise ValueError('invalid probe argv')
    return tuple(value)


def _server(value):
    value = dict(_mapping(value, {'transport', 'argv', 'url', 'bearer_env'}))
    transport = value.get('transport')
    if transport == 'stdio':
        if set(value) != {'transport', 'argv'}:
            raise ValueError('invalid stdio server configuration')
        value['argv'] = _argv(value['argv'])
    elif transport == 'streamable-http':
        if not {'transport', 'url'} <= set(value) or 'argv' in value:
            raise ValueError('invalid HTTP server configuration')
        try:
            url = urlsplit(value['url'])
            _ = url.port
            valid = (url.scheme == 'https' or
                     (url.scheme == 'http' and url.hostname in {'127.0.0.1', '::1', 'localhost'}))
            if (not valid or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment
                    or any(c.isspace() or ord(c) < 32 for c in value['url'])):
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            raise ValueError('invalid MCP URL; use HTTPS or loopback HTTP without credentials') from None
        if 'bearer_env' in value and not _name(value['bearer_env']):
            raise ValueError('invalid bearer environment reference')
    else:
        raise ValueError('unsupported MCP transport')
    return value


@asynccontextmanager
async def _connect(config, timeout=20):
    # Optional dependency: local executable probes do not import the SDK.
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    import httpx

    if config['transport'] == 'stdio':
        params = StdioServerParameters(command=config['argv'][0], args=list(config['argv'][1:]))
        # Provider stderr may contain secrets; do not forward it to server logs.
        with open(os.devnull, 'w') as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    else:
        headers = {}
        if 'bearer_env' in config:
            token = os.environ.get(config['bearer_env'])
            if not token or any(not 33 <= ord(c) <= 126 for c in token):
                raise MCPCredentialError('MCP credential unavailable')
            headers['Authorization'] = f'Bearer {token}'
        async with httpx.AsyncClient(headers=headers, follow_redirects=False, trust_env=False,
                                     timeout=timeout) as client:
            async with streamable_http_client(config['url'], http_client=client) as streams:
                async with ClientSession(*streams[:2]) as session:
                    await session.initialize()
                    yield session


async def _call(config, tool, arguments, output_limit, timeout=20):
    _install_log_redaction()
    logging_token = _probe_logging.set(True)
    try:
        # A fresh connection on each poll avoids stale sessions after failures.
        # Never retry within a poll: the next scheduled observation reconnects.
        async with _connect(config, timeout=timeout) as session:
            result = await session.call_tool(tool, arguments)
            if result.isError or not isinstance(result.structuredContent, dict):
                raise MCPProbeError('MCP probe failed')
            data = result.structuredContent
            if len(json.dumps(data, ensure_ascii=True, allow_nan=False).encode()) > output_limit:
                raise MCPProbeError('MCP probe result exceeds limit')
            return data
    except (asyncio.CancelledError, MCPCredentialError):
        raise
    except Exception:
        raise MCPProbeError('MCP probe failed') from None
    finally:
        _probe_logging.reset(logging_token)


def load_probe_registry(config):
    """Build adapters from a trusted mapping with `servers` and `probes` sections.

    Credentials are environment references, resolved on each connection. No API,
    topic memory, or watch payload may supply this configuration. Output limits
    bound accepted structured results, not SDK wire buffering.
    """
    config = _mapping(config, {'servers', 'probes'})
    servers = config.get('servers', {})
    probes = config.get('probes', {})
    if not isinstance(servers, dict) or not isinstance(probes, dict):
        raise ValueError('probe sections must be mappings')
    if any(not _name(name) for name in (*servers, *probes)):
        raise ValueError('invalid registry name')
    servers = {name: _server(value) for name, value in servers.items()}
    adapters = {}
    for name, value in probes.items():
        value = dict(_mapping(value, {'argv', 'server', 'tool', 'timeout', 'output_limit'}))
        if 'argv' in value:
            value['argv'] = _argv(value['argv'])
        probe = Probe(**value)
        if probe.server and probe.server not in servers:
            raise ValueError('unknown probe server')
        async def call(server, tool, arguments, *, registered=probe):
            if (server, tool) != (registered.server, registered.tool):
                raise ValueError('unregistered MCP operation')
            return await _call(servers[server], tool, arguments, registered.output_limit, registered.timeout)
        adapters[name] = ProbeAdapter(probe, call_tool=call if probe.server else None)
    return adapters


class MCPDiscoveryLimit(MCPProbeError):
    """The provider catalog exceeds the supported discovery bounds."""


_observation_sessions = ContextVar('watch_mcp_sessions', default=None)


@asynccontextmanager
async def observation_sessions():
    # Scope reuse and log redaction to one observation, including session cleanup.
    _install_log_redaction()
    logging_token = _probe_logging.set(True)
    try:
        async with AsyncExitStack() as stack:
            token = _observation_sessions.set((stack, {}))
            try:
                yield
            finally:
                _observation_sessions.reset(token)
                # Close without throwing a plan/authorization error through SDK
                # task groups, which otherwise wrap it in an ExceptionGroup.
                try:
                    await stack.aclose()
                except Exception:
                    raise MCPProbeError('MCP observation cleanup failed') from None
    finally:
        _probe_logging.reset(logging_token)


async def prepare_observation_connection(config):
    pool = _observation_sessions.get()
    if pool is not None:
        stack, sessions = pool
        key = json.dumps(config, sort_keys=True)
        if key not in sessions:
            # SDK cancel scopes must be entered/exited by the observation task,
            # not by the per-operation wait_for tasks below.
            try:
                sessions[key] = await stack.enter_async_context(_connect(config))
            except (asyncio.CancelledError, MCPCredentialError):
                raise
            except Exception:
                raise MCPProbeError('MCP observation connection failed') from None


@asynccontextmanager
async def _observation_connect(config):
    pool = _observation_sessions.get()
    if pool is None:
        async with _connect(config) as session:
            yield session
    else:
        _, sessions = pool
        yield sessions[json.dumps(config, sort_keys=True)]


async def discover_tools(config):
    """Bounded tool metadata discovery; never return provider exception text."""
    _install_log_redaction()
    token = _probe_logging.set(True)
    try:
        async def read():
            async with _observation_connect(config) as session:
                page = await session.list_tools()
                if page.nextCursor or len(page.tools) > 200:
                    raise MCPDiscoveryLimit('MCP tool discovery exceeds limit')
                tools = [dict(name=t.name, input_schema=t.inputSchema,
                              output_schema=getattr(t, 'outputSchema', None),
                              description=(getattr(t, 'description', None) or '')[:2048]) for t in page.tools]
                if len(json.dumps(tools, allow_nan=False).encode()) > 65536:
                    raise MCPDiscoveryLimit('MCP tool discovery exceeds limit')
                return tools
        return await asyncio.wait_for(read(), 20)
    except (asyncio.CancelledError, MCPCredentialError, MCPDiscoveryLimit):
        raise
    except Exception:
        raise MCPProbeError('MCP tool discovery failed') from None
    finally:
        _probe_logging.reset(token)


async def call_observation_tool(config, tool, arguments):
    """Ordinary MCP results may use structuredContent or a JSON text object."""
    _install_log_redaction()
    token = _probe_logging.set(True)
    try:
        async def read():
            async with _observation_connect(config) as session:
                result = await session.call_tool(tool, arguments)
                if result.isError:
                    raise MCPProbeError('MCP observation failed')
                data = result.structuredContent
                if data is None:
                    blocks = result.content
                    if len(blocks) != 1 or blocks[0].type != 'text' or len(blocks[0].text.encode()) > 65536:
                        raise MCPProbeError('MCP observation requires JSON object')
                    data = json.loads(blocks[0].text)
                if not isinstance(data, dict) or len(json.dumps(data, allow_nan=False).encode()) > 65536:
                    raise MCPProbeError('MCP observation requires bounded JSON object')
                return data
        return await asyncio.wait_for(read(), 20)
    except (asyncio.CancelledError, MCPCredentialError, MCPDiscoveryLimit):
        raise
    except Exception:
        raise MCPProbeError('MCP observation failed') from None
    finally:
        _probe_logging.reset(token)
