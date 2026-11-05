"""Configured native effort labels and argument templates; no shell evaluation."""
from typing import Optional

DEFAULT_EFFORT = {
    "codex": {"args": ["-c", 'model_reasoning_effort="{value}"'],
              "levels": {v: v.capitalize() for v in ("low", "medium", "high", "xhigh")}},
    "claudecode": {"args": ["--effort", "{value}"],
                   "levels": {v: v.capitalize() for v in ("low", "medium", "high", "xhigh", "max")}},
}

def validate_effort_config(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"args", "levels", "models", "providers"}:
        raise ValueError("effort must contain args, levels, and optional models/providers")
    if not raw:
        return {}
    args = raw.get("args")
    if not isinstance(args, list) or not args or not all(isinstance(a, str) and a for a in args) or not any("{value}" in a for a in args):
        raise ValueError("effort.args must be a non-empty argument list containing {value}")
    def levels(value):
        if not isinstance(value, dict) or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in value.items()):
            raise ValueError("effort levels must map native values to display labels")
    levels(raw.get("levels"))
    for key in ("models", "providers"):
        overrides = raw.get(key, {})
        if not isinstance(overrides, dict):
            raise ValueError(f"effort.{key} must be a mapping")
        for name, value in overrides.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"effort.{key} keys must be non-empty strings")
            levels(value)
    return raw

def effort_levels(harness: str, provider: Optional[str], model: Optional[str]) -> dict:
    from .harnesses import effort_config_for
    config = effort_config_for(harness)
    return config.get("models", {}).get(model, config.get("providers", {}).get(provider, config.get("levels", {})))

def effort_args(harness: str, provider: Optional[str], model: Optional[str], effort: Optional[str]) -> tuple[str, ...]:
    if not effort:
        return ()
    from .harnesses import effort_config_for
    if effort not in effort_levels(harness, provider, model):
        raise ValueError(f"Unsupported effort {effort!r} for {harness}/{provider}/{model or 'default'}")
    return tuple(arg.replace("{value}", effort) for arg in effort_config_for(harness)["args"])
