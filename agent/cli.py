"""Dispatch client commands before importing server configuration or UI state."""
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'search':
        from .history_cli import main as search_main
        raise SystemExit(search_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == 'route':
        from .route_cli import main as route_main
        raise SystemExit(route_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == 'mcp':
        from .shared_mcp import main as mcp_main
        raise SystemExit(mcp_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == 'continue':
        from .continuation_cli import main as continue_main
        raise SystemExit(continue_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in {'watch', 'tools'}:
        from .watch_cli import main as watch_main
        raise SystemExit(watch_main(sys.argv[1:]))
    from .server import main as server_main
    return server_main()
