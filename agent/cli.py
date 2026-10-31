"""Dispatch client commands before importing server configuration or UI state."""
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {'watch', 'tools'}:
        from .watch_cli import main as watch_main
        raise SystemExit(watch_main(sys.argv[1:]))
    from .server import main as server_main
    return server_main()
