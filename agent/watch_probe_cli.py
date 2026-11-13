"""Explicit local operator tooling for executable watch wrappers."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

import yaml

from .config_lock import config_file_lock


CONTRACT = {
    'stdin': {'target': '<watch target>', 'cursor': None},
    'stdout': {'revision': 'immutable-source-version', 'summary': 'optional text',
               'url': 'optional text', 'closed': False},
    'semantics': [
        'Read one JSON object from stdin; write exactly one JSON object to stdout and exit zero.',
        'revision is required: a nonempty string of at most 4096 characters identifying an immutable source version, including edits or attempts. Do not use recurring status labels or content hashes.',
        'Only revision, changed, summary, url and closed are accepted. changed/closed are booleans; summary/url are strings.',
        'Without changed, the first poll establishes a quiet baseline; later different revisions emit an event. changed:true allows an initial event; changed:false advances the baseline quietly. An unchanged revision never emits another event. Previously ingested revisions are deduplicated.',
        'closed:true marks the observation closed. Nonzero exit, invalid JSON/schema, timeout or excess output fails the poll without advancing the stored cursor; a later scheduled poll may retry.',
        'argv is executed directly without a shell. The wrapper inherits the host environment and working directory; use absolute paths and provision credentials on the host. Never put secrets in argv, targets or output. stderr is discarded.',
        'Default timeout is 20 seconds; output_limit defaults to 65536 bytes (maximum 65536), bounding both raw stdout and normalized JSON (sorted keys, compact separators, ASCII escapes). Watch timeout must exceed probe timeout plus 1 second cleanup allowance. Persisted events also have a 65536-byte JSON limit.',
    ],
    'registration': {'location': 'watches.registry.probes.<name> in the host squid.yaml',
                     'schema': {'argv': ['/absolute/path/to/wrapper'], 'timeout': 20, 'output_limit': 65536},
                     'effect': 'Restart host after registration; watches.enabled and policy must allow the adapter. Registration starts no polling and grants no authority.'},
    'next': 'Create a watch with adapter=<name>, target and request_key. Obtain an exact-watch monitoring grant and activate it. Local work requires a separate execution grant; publication requires explicitly activated publication scope and separate task_review.',
}


def main(argv):
    parser = argparse.ArgumentParser(prog='agentsquid watch probe',
        description='Local executable wrapper contract, test, and operator registration. No API credentials used.')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('contract', help='Print complete JSON wrapper contract and setup workflow')
    for action in ('test', 'register'):
        command = sub.add_parser(action, help='Execute one bounded sample poll' if action == 'test'
                                 else 'Register in an explicitly selected host YAML config; never activates watches')
        command.add_argument('--input', required=True, help='JSON executable registration file, or - for stdin')
        if action == 'test':
            command.add_argument('--target', required=True, help='Sample watch target')
            command.add_argument('--cursor', help='Previous revision; omitted means first poll')
        else:
            command.add_argument('--config', required=True, help='Existing host squid.yaml; operator access required')
            command.add_argument('--name', required=True, help='New adapter name; existing names are never replaced')
    args = parser.parse_args(argv)
    if args.action == 'contract':
        print(json.dumps(CONTRACT))
        return 0
    try:
        from .watch_mcp import load_probe_registry
        raw = json.loads(sys.stdin.read() if args.input == '-' else Path(args.input).read_text())
        adapter = load_probe_registry({'probes': {'wrapper': raw}})['wrapper']
        if not adapter.probe.argv:
            raise ValueError('executable registration required')
        if args.action == 'test':
            observation = asyncio.run(adapter.poll({'target': args.target, 'cursor': args.cursor}))
            print(json.dumps({'valid': True, 'request': {'target': args.target, 'cursor': args.cursor},
                              'observation': {'cursor': observation.cursor,
                                              'events': observation.events, 'closed': observation.closed}}))
        else:
            from .watch_config import load_watch_configuration
            path = Path(args.config)
            if path.is_symlink():
                raise ValueError('select the real config path')
            with config_file_lock(path):
                original = path.read_bytes()
                config = yaml.safe_load(original)
                if not isinstance(config, dict):
                    raise ValueError('config must be a mapping')
                load_watch_configuration(config)
                # Copy each edited mapping: YAML aliases may share these values
                # with unrelated configuration sections.
                section = dict(config.get('watches') or {})
                config['watches'] = section
                registry = dict(section.get('registry', {}))
                section['registry'] = registry
                probes = dict(registry.get('probes', {}))
                registry['probes'] = probes
                if args.name in probes:
                    raise ValueError('adapter already registered')
                probes[args.name] = raw
                load_watch_configuration(config)
                # Atomic replacement preserves permissions; reject concurrent edits.
                temp = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                        temp = Path(stream.name)
                        os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
                        stream.write(yaml.safe_dump(config, sort_keys=False).encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                    if path.read_bytes() != original:
                        raise ValueError('config changed during registration')
                    os.replace(temp, path)
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    if temp is not None:
                        temp.unlink(missing_ok=True)
            print(json.dumps({'registered': args.name, 'restart_required': True,
                              'activated': False}))
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, asyncio.TimeoutError, yaml.YAMLError):
        # Provider exceptions and output may contain credentials.
        print(json.dumps({'error': 'watch_probe_' + args.action + '_failed'}), file=sys.stderr)
        return 1
