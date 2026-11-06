"""Bounded declarative observation plans; no generated code or ambient tools."""
import asyncio
import hashlib
import json
import inspect
from pathlib import Path
import sys

from . import watch_mcp
from .watches import Observation, encode_event_payload


MAX_RECORDS = 500


PLAN_ERRORS = {
    'plan_schema_timeout': 'The tool schema exceeded its validation budget; simplify the schema or use another read tool.',
    'plan_discovery_limit': 'The MCP tool catalog is paginated or too large; expose a smaller read-tool catalog.',
    'plan_credentials_required': 'Restore the host connection credential or configure its named bearer environment variable.',
    'plan_connection_changed': 'Revalidate the watch after its host connection or read permissions change.',
    'plan_schema_reference_unsupported': 'Use a self-contained tool schema with only local JSON references.',
    'plan_connection_required': 'Connect a host MCP service under watches.connections; session-only tools are unavailable after exit.',
    'plan_read_permission_required': 'Ask the host operator to approve the required read tool on this connection.',
    'plan_connection_unavailable': 'Check the host MCP endpoint and its configured credentials.',
    'plan_timeout': 'Reduce plan steps or fix the slow MCP connection.',
    'plan_field_missing': 'Choose a field that exists in the actual tool response.',
    'plan_incomplete_read': 'The source reports more data; this version cannot paginate. Use a complete bounded read.',
    'plan_tool_schema_changed': 'Rebuild and revalidate this watch against the current tool schema.',
    'plan_arguments_invalid': 'Match the arguments to the discovered input schema.',
    'plan_tool_unavailable': 'Reconnect the service or select an available approved read tool.',
    'plan_source_identity_required': 'Select stable source IDs plus immutable edit versions or run attempt IDs.',
    'plan_record_limit': 'Narrow the source query to at most 500 records; do not truncate silently.',
    'plan_record_limit_or_duplicate_identity': 'Use unique source versions and at most 500 matching records.',
    'plan_invalid_json': 'Supply finite JSON values.',
    'plan_size_limit': 'Keep plan JSON within 32768 bytes.',
    'plan_invalid_schema': 'Use version 1, initial quiet/actionable and one to four steps.',
    'plan_invalid_step': 'Use the supported step fields from watch plan documentation.',
    'plan_invalid_path': 'Use a bounded JSON Pointer path.',
    'plan_unsupported_predicate': 'Use a path/equals predicate with a scalar JSON value.',
    'plan_mcp_dependency_required': 'Install agentsquid[mcp] on the host.',
    'plan_cursor_invalid': 'Pause and recreate the watch; its baseline is invalid.',
}


# Delivered on demand so agents can construct plans without reading host files.
PLAN_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['version', 'initial', 'steps'],
    'properties': {
        'version': {'const': 1}, 'initial': {'enum': ['quiet', 'actionable']},
        'steps': {'type': 'array', 'minItems': 1, 'maxItems': 4, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['server', 'tool', 'arguments', 'identity'],
            'properties': {
                'server': {'type': 'string'}, 'tool': {'type': 'string'},
                'arguments': {'type': 'object'},
                'items': {'type': 'string', 'description': 'JSON Pointer to the record array; omit for a singleton response.'},
                'identity': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                             'items': {'type': 'string'},
                             'description': 'Record JSON Pointers identifying immutable source occurrences: IDs plus edit versions or run attempts, never status alone.'},
                **{key: {'type': 'object', 'additionalProperties': False,
                         'required': ['path', 'equals'], 'properties': {
                             'path': {'type': 'string'},
                             'equals': {'type': ['string', 'number', 'boolean', 'null']}}}
                   for key in ('when', 'stop', 'complete')},
            },
        }},
    },
}


class PlanError(ValueError):
    """Only fixed, safe codes may cross the API/log boundary."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def pointer(value, path):
    try:
        if path == '':
            return value
        for part in path[1:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not part.isdecimal():
                    raise KeyError()
                value = value[int(part)]
            else:
                value = value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        raise PlanError('plan_field_missing') from None


def _path(path):
    if not isinstance(path, str) or len(path) > 512 or (path and not path.startswith('/')):
        raise PlanError('plan_invalid_path')


def _predicate(raw):
    if not isinstance(raw, dict) or set(raw) != {'path', 'equals'}:
        raise PlanError('plan_unsupported_predicate')
    _path(raw['path'])
    if not (raw['equals'] is None or type(raw['equals']) in (str, bool, int, float)):
        raise PlanError('plan_unsupported_predicate')


def matches(value, rule):
    actual = pointer(value, rule['path'])
    return type(actual) is type(rule['equals']) and actual == rule['equals']


def validate(plan):
    try:
        encoded = canonical(plan)
    except (ValueError, TypeError, RecursionError):
        raise PlanError('plan_invalid_json') from None
    if len(encoded.encode()) > 32768:
        raise PlanError('plan_size_limit')
    if (not isinstance(plan, dict) or set(plan) != {'version', 'initial', 'steps'}
            or type(plan['version']) is not int or plan['version'] != 1
            or plan['initial'] not in ('quiet', 'actionable')
            or not isinstance(plan['steps'], list) or not 1 <= len(plan['steps']) <= 4):
        raise PlanError('plan_invalid_schema')
    for step in plan['steps']:
        if (not isinstance(step, dict) or set(step) - {
                'server', 'tool', 'arguments', 'items', 'identity', 'when', 'stop', 'complete'}
                or not {'server', 'tool', 'arguments', 'identity'} <= set(step)
                or not watch_mcp._name(step['server']) or not watch_mcp._name(step['tool'])
                or not isinstance(step['arguments'], dict)
                or not isinstance(step['identity'], list) or not 1 <= len(step['identity']) <= 8):
            raise PlanError('plan_invalid_step')
        for path in step['identity']:
            _path(path)
        if 'items' in step:
            _path(step['items'])
        for field in ('when', 'stop', 'complete'):
            if field in step:
                _predicate(step[field])
    return json.loads(encoded)  # detach caller-owned mutable values


async def _authorize(callback):
    if callback is not None:
        result = callback()
        if inspect.isawaitable(result):
            await result


async def validate_arguments(schema, arguments):
    from .watch_probes import Probe, ProbeAdapter
    worker = ProbeAdapter(Probe(argv=(sys.executable, str(Path(__file__).with_name('watch_schema.py')))))
    try:
        result = await asyncio.wait_for(worker._execute(dict(schema=schema, arguments=arguments)), 3)
    except asyncio.TimeoutError:
        raise PlanError('plan_schema_timeout') from None
    if result['error'] is not None:
        raise PlanError(result['error'])


async def _read(operation):
    try:
        return await operation
    except watch_mcp.MCPDiscoveryLimit:
        raise PlanError('plan_discovery_limit') from None
    except watch_mcp.MCPCredentialError:
        raise PlanError('plan_credentials_required') from None
    except watch_mcp.MCPProbeError:
        raise PlanError('plan_connection_unavailable') from None


class PlanAdapter:
    """All calls use host-configured connections and operator-approved tool names."""
    def __init__(self, connections):
        self.connections = connections

    def check(self, plan):
        plan = validate(plan)
        for step in plan['steps']:
            connection = self.connections.get(step['server'])
            if connection is None:
                raise PlanError('plan_connection_required')
            if step['tool'] not in connection['read_tools']:
                raise PlanError('plan_read_permission_required')
        return plan

    def connection_digests(self, plan):
        return [digest(dict(connection=self.connections[s['server']]['connection'],
                            read_tools=sorted(self.connections[s['server']]['read_tools'])))
                for s in plan['steps']]

    async def discover(self, *, authorize=None):
        operations, unavailable = [], []
        for name, connection in self.connections.items():
            await _authorize(authorize)
            try:
                tools = await watch_mcp.discover_tools(connection['connection'])
                for tool in tools:
                    if tool['name'] in connection['read_tools']:
                        operations.append(dict(server=name, **tool))
            except watch_mcp.MCPDiscoveryLimit:
                unavailable.append(dict(server=name, error='plan_discovery_limit'))
            except watch_mcp.MCPProbeError:
                unavailable.append(dict(server=name, error='plan_connection_unavailable'))
        return dict(operations=operations, unavailable=unavailable,
                    plan_version=1, plan_schema=PLAN_SCHEMA, max_steps=4, max_records=MAX_RECORDS,
                    workflow=['discover', 'validate', 'create proposal', 'activate with exact-watch grant'],
                    field_scopes={'when': 'each record', 'stop': 'whole response; any step may stop',
                                  'complete': 'whole response; false rejects the entire read'},
                    result_formats=['structured_object', 'json_text_object'],
                    pagination=False, bindings=False,
                    predicates=['equals'], paths='JSON Pointer',
                    setup='Connect a host MCP service and approve its read tools; session-only tools cannot be polled.')

    async def evaluate(self, plan, *, cursor=None, schemas=None, authorize=None):
        async with watch_mcp.observation_sessions():
            return await self._evaluate(plan, cursor=cursor, schemas=schemas, authorize=authorize)

    async def _evaluate(self, plan, *, cursor=None, schemas=None, authorize=None):
        plan = self.check(plan)
        events, fingerprints, closed = [], [], False
        catalogs = {}
        for index, step in enumerate(plan['steps']):
            await _authorize(authorize)
            connection = self.connections[step['server']]['connection']
            await _read(watch_mcp.prepare_observation_connection(connection))
            await _authorize(authorize)
            if step['server'] not in catalogs:
                catalogs[step['server']] = await _read(watch_mcp.discover_tools(connection))
            tools = catalogs[step['server']]
            tool = next((t for t in tools if t['name'] == step['tool']), None)
            if tool is None:
                raise PlanError('plan_tool_unavailable')
            fingerprint = digest(dict(input=tool['input_schema'], output=tool.get('output_schema')))
            fingerprints.append(fingerprint)
            if schemas is not None and (index >= len(schemas) or schemas[index] != fingerprint):
                raise PlanError('plan_tool_schema_changed')
            await validate_arguments(tool['input_schema'], step['arguments'])
            await _authorize(authorize)
            result = await _read(watch_mcp.call_observation_tool(connection, step['tool'], step['arguments']))
            if 'complete' in step and not matches(result, step['complete']):
                raise PlanError('plan_incomplete_read')
            if 'stop' in step:
                closed |= matches(result, step['stop'])
            records = pointer(result, step['items']) if 'items' in step else [result]
            if not isinstance(records, list) or len(records) > MAX_RECORDS:
                raise PlanError('plan_record_limit')
            for record in records:
                identity = [pointer(record, path) for path in step['identity']]
                if any(type(v) not in (str, int) or v == '' for v in identity):
                    raise PlanError('plan_source_identity_required')
                key = 'plan:' + digest([index, identity])
                if 'when' not in step or matches(record, step['when']):
                    payload = dict(source=step['server'], tool=step['tool'], identity=identity, data=record)
                    encode_event_payload(payload)
                    events.append((key, payload))
        if len(events) > MAX_RECORDS or len({k for k, _ in events}) != len(events):
            raise PlanError('plan_record_limit_or_duplicate_identity')
        # Keep the initial quiet baseline permanently. Later replay is deduplicated
        # by the durable event inbox, including after records disappear/reappear.
        if cursor is None:
            baseline = [k for k, _ in events] if plan['initial'] == 'quiet' else []
        else:
            try:
                baseline = json.loads(cursor)
            except (ValueError, TypeError):
                raise PlanError('plan_cursor_invalid') from None
        observation = Observation(canonical(baseline), tuple((k, p) for k, p in events if k not in baseline), closed)
        return observation, fingerprints

    async def poll_authorized(self, watch, authorize):
        return await self.poll(watch, authorize=authorize)

    async def poll(self, watch, *, authorize=None):
        saved = json.loads(watch['observation_plan'])
        self.check(saved['plan'])
        if saved['connections'] != self.connection_digests(saved['plan']):
            raise PlanError('plan_connection_changed')
        async def run():
            observation, _ = await self.evaluate(saved['plan'], cursor=watch['cursor'], schemas=saved['schemas'], authorize=authorize)
            return observation
        try:
            return await asyncio.wait_for(run(), 20)
        except asyncio.TimeoutError:
            raise PlanError('plan_timeout') from None
