"""Test-only stdio/Streamable HTTP MCP source. State is supplied by the owning test process."""
import argparse
import asyncio
import json
import math
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


def make_server(state_path: Path, *, port=8000):
    server = FastMCP('Squid watch fixture', host='127.0.0.1', port=port)

    @server.tool()
    async def task_snapshot(target: str, cursor: str | None = None) -> dict[str, Any]:
        """Read a fixture task; revisions identify source occurrences, not statuses."""
        state = json.loads(state_path.read_text())
        task = state['tasks'].get(target)
        if task is None:
            raise ValueError('fixture target not found')
        delay = task.get('delay', 0)
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 30:
            raise ValueError('invalid fixture delay')
        await asyncio.sleep(delay)
        if task.get('error'):
            raise RuntimeError('fixture provider failure')
        return {key: value for key, value in task.items()
                if key in {'revision', 'changed', 'summary', 'url', 'closed'}}

    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--transport', choices=['stdio', 'streamable-http'], default='stdio')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    make_server(args.state, port=args.port).run(transport=args.transport)
