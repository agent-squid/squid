"""Validated, portable runtime context assembled after execution facts resolve."""
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
import re
from typing import Mapping, Optional

import yaml


_CONDITIONS = {
    "protocol": {"oneshot-cli", "interactive-cli", "interactive-pty"},
    "worktree_isolated": {True, False},
    "native_shell": {True, False},
    "has_code_roots": {True, False},
}
_VARIABLE = re.compile(r"{{\s*([a-z_][a-z_0-9]*)\s*}}")
_CADENCES = {"every-turn", "session-start", "this-request"}


@dataclass(frozen=True)
class ContextBlock:
    id: str
    owner: str
    source: str
    required: bool
    inject: str
    revision: str
    variables: dict[str, str]
    content: str
    reason: str = "execution conditions matched"
    delivery_channel: str = "prompt"


@dataclass(frozen=True)
class RuntimeTemplate:
    id: str
    source: str
    inject: str
    when: dict
    variables: tuple[str, ...]
    body: str
    revision: str

    def render(self, values: Mapping[str, str]) -> ContextBlock:
        missing = set(self.variables) - values.keys()
        if missing or any(not isinstance(values[k], str) for k in self.variables):
            raise ValueError(f"{self.id}: missing or invalid variables: {sorted(missing)}")
        used = {key: values[key] for key in self.variables}
        content = _VARIABLE.sub(lambda match: used[match[1]], self.body)
        return ContextBlock(self.id, "squid", self.source, True, self.inject,
                            self.revision, used, content)


def load_template(text: str, source: str) -> RuntimeTemplate:
    """Reject unsupported metadata and syntax rather than omitting policy."""
    parts = text.split("---", 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError(f"{source}: required YAML frontmatter missing")
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise ValueError(f"{source}: invalid frontmatter") from exc
    fields = {"id", "owner", "inject", "required", "when", "variables"}
    if not isinstance(meta, dict) or set(meta) != fields:
        raise ValueError(f"{source}: invalid metadata fields")
    if (not isinstance(meta['id'], str) or not meta['id'].startswith('runtime.')
            or meta['owner'] != 'squid' or meta['required'] is not True
            or not isinstance(meta['inject'], str) or meta['inject'] not in _CADENCES):
        raise ValueError(f"{source}: invalid runtime identity or cadence")
    conditions = meta['when']
    if not isinstance(conditions, dict):
        raise ValueError(f"{source}: invalid conditions")
    for key, value in conditions.items():
        if key not in _CONDITIONS or type(value) not in (str, bool) or value not in _CONDITIONS[key]:
            raise ValueError(f"{source}: unsupported condition {key}")
    variables = meta['variables']
    body = parts[2].strip()
    if (not isinstance(variables, list)
            or any(not isinstance(v, str) or not re.fullmatch('[a-z_][a-z_0-9]*', v) for v in variables)
            or len(set(variables)) != len(variables)
            or set(_VARIABLE.findall(body)) != set(variables)
            or '{{' in _VARIABLE.sub('', body) or '}}' in _VARIABLE.sub('', body)):
        raise ValueError(f"{source}: invalid or undeclared substitutions")
    return RuntimeTemplate(meta['id'], source, meta['inject'], conditions,
                           tuple(variables), body, sha256(text.encode()).hexdigest())


def select_blocks(templates, *, state: Mapping, values: Mapping[str, str],
                  delivered: Optional[Mapping[str, str]] = None,
                  selected=(), reset: bool = False) -> list[ContextBlock]:
    """Pure selection; callers record revisions only after accepted delivery.

    No delivery history means a stateless invocation or uncertain continuity.
    """
    blocks = []
    ids = set()
    for template in templates:
        if template.id in ids:
            raise ValueError(f"duplicate context id: {template.id}")
        ids.add(template.id)
        if any(key not in state or state[key] != value for key, value in template.when.items()):
            continue
        if template.inject == 'this-request' and template.id not in selected:
            continue
        if (template.inject == 'session-start' and not reset and delivered is not None
                and delivered.get(template.id) == template.revision):
            continue
        blocks.append(template.render(values))
    return blocks


@lru_cache(maxsize=1)
def runtime_templates() -> tuple[RuntimeTemplate, ...]:
    """Load and validate packaged policy once per process, before serving turns."""
    directory = files('agent').joinpath('runtime')
    templates = []
    for name in ('oneshot', 'global', 'worktree'):
        template = load_template(
            directory.joinpath(name + '.md').read_text(encoding='utf-8'),
            f'agent/runtime/{name}.md',
        )
        if template.id != f'runtime.{name}':
            raise ValueError(f'{template.source}: unexpected runtime id {template.id}')
        # Validate against the values assembly can actually supply, even when
        # this template does not apply to the first turn served.
        template.render({'code_roots': '', 'backing_repos': ''})
        templates.append(template)
    return tuple(templates)


def runtime_prompt(blocks: list[ContextBlock], prompt: str) -> str:
    """Preserve the legacy separation of lifecycle and code-root guidance."""
    parts = []
    for block in blocks:
        if block.id == 'runtime.worktree' and parts:
            parts[-1] += '\n' + block.content
        else:
            parts.append(block.content)
    return '\n\n'.join(parts + [prompt])


def assemble_runtime(*, protocol: str = '', native_shell: bool = False,
                     code_roots=None, worktree_isolated: bool = False,
                     backing_roots=None) -> list[ContextBlock]:
    roots = list(code_roots or [])
    backing = list(backing_roots or []) if worktree_isolated else []
    root_lines = ([f'{root}  (repo: {repo})' for root, repo in zip(roots, backing)]
                  if worktree_isolated and len(roots) == len(backing) else roots)
    values = {
        'code_roots': '\n'.join(root_lines),
        'backing_repos': ('Backing git repos:\n<squid_backing_repos>\n'
                          + '\n'.join(backing) + '\n</squid_backing_repos>\n') if backing else '',
    }
    state = dict(protocol=protocol, native_shell=native_shell,
                 worktree_isolated=worktree_isolated, has_code_roots=bool(roots))
    return select_blocks(runtime_templates(), state=state, values=values)
