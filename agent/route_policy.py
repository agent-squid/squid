"""Finite host-owned route rollout policy; no runtime imports."""
DEFAULTS = dict(max_turns=16, max_depth=4, max_seconds=1800, turn_seconds=300,
                concurrency=4, topic_concurrency=8, global_concurrency=32)

CAPS = dict(max_turns=64, max_depth=8, max_seconds=86400, turn_seconds=3600,
            concurrency=16, topic_concurrency=32, global_concurrency=128)


def configuration(config):
    value = config.get('routes', {})
    if not isinstance(value, dict) or set(value) - {'enabled', *DEFAULTS}:
        raise ValueError('invalid routes configuration')
    if type(value.get('enabled', False)) is not bool:
        raise ValueError('routes.enabled must be boolean')
    result = dict(enabled=value.get('enabled', False))
    for key, default in DEFAULTS.items():
        number = value.get(key, default)
        if type(number) is not int or not 1 <= number <= CAPS[key]:
            raise ValueError('routes.' + key + ' must be a positive bounded integer')
        result[key] = number
    return result

