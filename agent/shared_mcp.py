"""Optional stdio adapter for the same turn-scoped HTTP operations as the CLI.

No credentials in tool arguments and no persistent harness configuration writes.
"""
import argparse
import asyncio
import json
import sys

import httpx

from . import shared_tools


async def invoke(name, arguments):
    if name not in shared_tools.OPERATIONS:
        return dict(error='unknown_shared_tool')
    try:
        return await asyncio.to_thread(shared_tools.request, name, arguments)
    except (ValueError, OSError, httpx.HTTPError):
        return dict(error='shared_client_error')


def server():
    # Optional dependency; importing CLI/HTTP code never imports the MCP SDK.
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, TextContent, Tool
    app = Server('agentsquid')

    @app.list_tools()
    async def list_tools():
        return [Tool(name=item['name'], description='Squid turn-scoped ' + item['name'].replace('_', ' '),
                     inputSchema=item['input_schema']) for item in shared_tools.catalog()]

    @app.call_tool()
    async def call_tool(name, arguments):
        value = await invoke(name, arguments)
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(value))],
                              isError=isinstance(value, dict) and 'error' in value)
    return app


async def run():
    from mcp.server.stdio import stdio_server
    app = server()
    async with stdio_server() as streams:
        await app.run(*streams, app.create_initialization_options())


def main(argv):
    parser = argparse.ArgumentParser(prog='agentsquid mcp', description=
        'Optional stdio adapter for Squid history, route and continuation tools. Uses the issuing turn\'s '
        'SQUID_API_URL and SQUID_CONTINUE_TOKEN environment; does not change harness configuration. '
        'Install the mcp extra to use this transport. CLI remains available without it.')
    parser.parse_args(argv)
    try:
        asyncio.run(run())
        return 0
    except ImportError:
        print('{"error":"mcp_dependency_unavailable"}', file=sys.stderr)
        return 1
