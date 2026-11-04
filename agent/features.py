"""Release feature switches from squid.yaml; restart after changing."""
from . import config


def shore_enabled() -> bool:
    return config.shore_enabled(config._cfg)
