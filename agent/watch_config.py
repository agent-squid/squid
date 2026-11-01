"""Trusted host configuration only; loading grants no watch authority."""
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping

from .watch_policy import WatchPolicy, parse_policy
from .watch_mcp import load_probe_registry
from .watches import Adapter


@dataclass(frozen=True)
class WatchConfiguration:
    enabled: bool
    adapters: dict[str, Adapter]
    policy: WatchPolicy
    topics: Mapping[str, WatchPolicy]

    def effective_policy(self, topic):
        policy = self.policy.narrow(self.topics.get(topic, self.policy))
        return replace(policy, enabled=self.enabled and policy.enabled)


def load_watch_configuration(config: dict) -> WatchConfiguration:
    """Validate even disabled registrations, without opening any transports.

    The enabled switch makes adapters available to the authorized observation
    worker. Loading alone does not start polling, activate watches, or authorize
    mutations; the server lifespan owns the worker.
    """
    section = config.get('watches', {})
    if section is None:
        section = {}
    if not isinstance(section, dict) or set(section) - {'enabled', 'registry', 'policy', 'topics', 'connections'}:
        raise ValueError('invalid watches configuration fields')
    enabled = section.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('watches.enabled must be a boolean')
    adapters = load_probe_registry(section.get('registry', {}))
    if 'plan' in adapters:
        raise ValueError('plan is a reserved watch adapter name')
    from .watch_mcp import _server, _name
    from .watch_plans import PlanAdapter
    raw_connections = section.get('connections', {})
    if not isinstance(raw_connections, dict):
        raise ValueError('watch connections must be a mapping')
    connections = {}
    for name, raw in raw_connections.items():
        if (not _name(name) or not isinstance(raw, dict)
                or set(raw) != {'connection', 'read_tools'}
                or not isinstance(raw['read_tools'], list)
                or any(not _name(tool) for tool in raw['read_tools'])):
            raise ValueError('invalid watch connection')
        connections[name] = dict(connection=_server(raw['connection']),
                                 read_tools=frozenset(raw['read_tools']))
    if connections:
        adapters['plan'] = PlanAdapter(connections)
    defaults = WatchPolicy(adapters=frozenset(adapters))
    policy = parse_policy(section.get('policy', {}), defaults=defaults)
    topics = section.get('topics', {})
    if (not isinstance(topics, dict) or
            any(not isinstance(name, str) or not name.strip() for name in topics)):
        raise ValueError('watch topics must be a mapping of topic names')
    narrowed = {name: policy.narrow(parse_policy(raw, defaults=policy))
                for name, raw in topics.items()}
    return WatchConfiguration(enabled, adapters if enabled else {}, policy,
                              MappingProxyType(narrowed))
