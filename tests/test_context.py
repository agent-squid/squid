from dataclasses import replace
from importlib.resources import files

import pytest

from agent.context import assemble_runtime, load_template, select_blocks


def template():
    return load_template(files('agent').joinpath('runtime/oneshot.md').read_text(), 'oneshot.md')


def test_runtime_order_and_fresh_isolation_facts():
    for turn in ('first', 'second'):
        blocks = assemble_runtime(protocol='oneshot-cli', code_roots=[f'/isolated/{turn}'],
                                  backing_roots=['/repo'], worktree_isolated=True)
        assert [b.id for b in blocks] == ['runtime.oneshot', 'runtime.global', 'runtime.worktree']
        assert f'/isolated/{turn}  (repo: /repo)' in blocks[1].content
        assert '/repo' in blocks[2].content
        assert all(b.required and b.owner == 'squid' and b.delivery_channel == 'prompt' for b in blocks)
        assert blocks[1].variables['code_roots'].startswith(f'/isolated/{turn}')


def test_native_shell_has_no_prose_and_interactive_has_no_oneshot():
    assert assemble_runtime(protocol='oneshot-cli', native_shell=True,
                            code_roots=['/repo'], worktree_isolated=True) == []
    assert [b.id for b in assemble_runtime(protocol='interactive-cli', code_roots=['/repo'])] == ['runtime.global']


def test_cadence_revision_reset_and_stateless():
    t = replace(template(), inject='session-start')
    kwargs = dict(state={'protocol': 'oneshot-cli', 'native_shell': False}, values={})
    history = {t.id: t.revision}
    assert select_blocks([t], **kwargs)
    assert select_blocks([t], **kwargs, delivered=history) == []
    assert select_blocks([t], **kwargs, delivered=history, reset=True)
    assert select_blocks([replace(t, revision='changed')], **kwargs, delivered=history)
    t = replace(t, inject='this-request')
    assert select_blocks([t], **kwargs) == []
    assert select_blocks([t], **kwargs, selected=[t.id])
    assert history == {t.id: t.revision}


@pytest.mark.parametrize('old,new', [
    ('every-turn', 'sometimes'), ('native_shell', 'unknown'),
    ('oneshot-cli', 'unknown'), ('owner: squid', 'owner: user'),
    ('required: true', 'required: false'), ('variables: []', 'variables: [missing]'),
])
def test_invalid_metadata_fails(old, new):
    text = files('agent').joinpath('runtime/oneshot.md').read_text()
    with pytest.raises(ValueError):
        load_template(text.replace(old, new), 'bad.md')


@pytest.mark.parametrize('syntax', ['{{undeclared}}', '{{env.SECRET}}', '{{include file}}'])
def test_unsupported_substitution_fails(syntax):
    text = files('agent').joinpath('runtime/oneshot.md').read_text()
    with pytest.raises(ValueError):
        load_template(text + syntax, 'bad.md')


def test_missing_required_value_and_duplicate_ids_fail():
    t = load_template(files('agent').joinpath('runtime/global.md').read_text(), 'global.md')
    with pytest.raises(ValueError):
        t.render({})
    with pytest.raises(ValueError, match='duplicate'):
        select_blocks([t, t], state={'has_code_roots': True, 'native_shell': False},
                      values={'code_roots': '/repo'})


def test_runtime_templates_cached_but_values_render_each_turn(monkeypatch):
    from agent import context
    context.runtime_templates.cache_clear()
    original = context.load_template
    loaded = []

    def track(text, source):
        loaded.append(source)
        return original(text, source)

    monkeypatch.setattr(context, 'load_template', track)
    try:
        first = assemble_runtime(code_roots=['/first'])
        second = assemble_runtime(code_roots=['/second'])
        assert len(loaded) == 3
        assert '/first' in first[0].content
        assert '/second' in second[0].content
    finally:
        context.runtime_templates.cache_clear()


@pytest.mark.parametrize('damage', ['metadata', 'identity', 'variable'])
def test_bad_packaged_template_prevents_startup(monkeypatch, tmp_path, damage):
    import asyncio
    from agent import context, server

    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    for name in ('oneshot', 'global', 'worktree'):
        text = files('agent').joinpath(f'runtime/{name}.md').read_text()
        if name == 'worktree':
            if damage == 'metadata':
                text = text.replace('every-turn', 'invalid')
            elif damage == 'identity':
                text = text.replace('runtime.worktree', 'runtime.oneshot')
            else:
                text = text.replace('backing_repos', 'unknown_variable')
        (runtime / f'{name}.md').write_text(text)
    context.runtime_templates.cache_clear()
    monkeypatch.setattr(context, 'files', lambda package: tmp_path)
    started = []
    monkeypatch.setattr(server._realtime_notifier, 'start', lambda loop: started.append(True))

    async def start():
        async with server._lifespan(server.app):
            pytest.fail('Invalid template allowed server startup')

    try:
        with pytest.raises(ValueError):
            asyncio.run(start())
        assert started == []
    finally:
        context.runtime_templates.cache_clear()


def test_runtime_prompt_preserves_legacy_spacing():
    from agent.context import runtime_prompt
    from agent.memory import code_roots_prompt_block, oneshot_protocol_prompt_block
    for isolated, backing in ((False, []), (True, ['/repo']), (True, []), (True, ['/one', '/two'])):
        blocks = assemble_runtime(protocol='oneshot-cli', code_roots=['/work'],
                                  worktree_isolated=isolated, backing_roots=backing)
        expected = '\n\n'.join([
            oneshot_protocol_prompt_block(),
            code_roots_prompt_block(['/work'], isolated=isolated, backing_roots=backing),
            'request',
        ])
        assert runtime_prompt(blocks, 'request') == expected
    assert runtime_prompt([], 'request') == 'request'
    assert runtime_prompt(assemble_runtime(protocol='oneshot-cli'), 'request') == (
        oneshot_protocol_prompt_block() + '\n\nrequest')


def test_oneshot_helper_selects_by_id(monkeypatch):
    from agent import context, memory
    blocks = assemble_runtime(protocol='oneshot-cli', code_roots=['/repo'])
    monkeypatch.setattr(context, 'assemble_runtime', lambda **kwargs: list(reversed(blocks)))
    assert memory.oneshot_protocol_prompt_block() == blocks[0].content
