"""Trusted host configuration only; loading grants no watch authority."""
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping

from .watch_policy import WatchPolicy, parse_policy
from .watch_mcp import load_probe_registry
from .watch_probes import ProbeAdapter


@dataclass(frozen=True)
class WatchConfiguration:
    enabled: bool
    adapters: dict[str, ProbeAdapter]
    policy: WatchPolicy
    topics: Mapping[str, WatchPolicy]

    def effective_policy(self, topic):
        policy = self.policy.narrow(self.topics.get(topic, self.policy))
        return replace(policy, enabled=self.enabled and policy.enabled)


def load_watch_configuration(config: dict) -> WatchConfiguration:
    """Validate even disabled registrations, without opening any transports.

    The enabled switch makes adapters available to future authorized services;
    it does not start polling, activate watches, or authorize mutations.
    """
    section = config.get('watches', {})
    if not isinstance(section, dict) or set(section) - {'enabled', 'registry', 'policy', 'topics'}:
        raise ValueError('invalid watches configuration fields')
    enabled = section.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('watches.enabled must be a boolean')
    adapters = load_probe_registry(section.get('registry', {}))
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
