"""Strict, observation-only policy. Topic settings can only narrow global policy."""
from dataclasses import dataclass

OPERATIONS = frozenset({'create', 'list', 'show', 'activate', 'pause', 'resume', 'cancel'})


@dataclass(frozen=True)
class WatchPolicy:
    enabled: bool = True
    operations: frozenset[str] = OPERATIONS
    adapters: frozenset[str] = frozenset()
    min_poll_seconds: int = 30
    max_lifetime_seconds: int = 86400

    def narrow(self, other):
        return WatchPolicy(self.enabled and other.enabled,
                           self.operations & other.operations,
                           self.adapters & other.adapters,
                           max(self.min_poll_seconds, other.min_poll_seconds),
                           min(self.max_lifetime_seconds, other.max_lifetime_seconds))


def parse_policy(raw, *, defaults):
    fields = {'enabled', 'operations', 'adapters', 'min_poll_seconds', 'max_lifetime_seconds'}
    if not isinstance(raw, dict) or set(raw) - fields:
        raise ValueError('invalid watch policy fields')
    values = dict(enabled=defaults.enabled, operations=defaults.operations,
                  adapters=defaults.adapters, min_poll_seconds=defaults.min_poll_seconds,
                  max_lifetime_seconds=defaults.max_lifetime_seconds)
    for key, value in raw.items():
        if key == 'enabled':
            if type(value) is not bool:
                raise ValueError('watch policy enabled must be boolean')
        elif key in {'operations', 'adapters'}:
            if (not isinstance(value, list) or
                    any(not isinstance(v, str) or not v.strip() for v in value)):
                raise ValueError('watch policy allowlists must be string lists')
            value = frozenset(value)
            if key == 'operations' and not value <= OPERATIONS:
                raise ValueError('unsupported watch operation')
            if key == 'adapters' and not value <= defaults.adapters:
                raise ValueError('unregistered watch policy adapter')
        elif type(value) is not int or value <= 0:
            raise ValueError('watch policy bounds must be positive integers')
        values[key] = value
    if values['min_poll_seconds'] < 30:
        raise ValueError('watch polling minimum is 30 seconds')
    return WatchPolicy(**values)
