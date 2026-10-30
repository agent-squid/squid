"""Trusted, explicit probe registry. Loading it never starts polling or a client.

Only host-owned configuration may reach this module. Registrations declare read
operations; tool discovery is not authority to invoke additional tools.
"""
import asyncio
from contextlib import asynccontextmanager
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
