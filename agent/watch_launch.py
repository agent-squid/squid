"""Launch-scoped watch credentials. Persistent harnesses remain unsupported."""
import logging
from dataclasses import asdict
from importlib.resources import files
import os
from pathlib import Path
import shutil
import sys
from contextlib import aclosing
from uuid import uuid4

from .context import load_template
from .watch_async import call
from .watch_urls import valid_api_url
from .watch_service import WatchControl

log = logging.getLogger(__name__)

_service = None
_api_url = None


def launch_instruction(name):
    source = f'agent/runtime/launch-{name}.md'
    content = files('agent').joinpath(f'runtime/launch-{name}.md').read_text(encoding='utf-8')
    return dict(asdict(load_template(content, source).render({})), delivery='injected')


def configure(service, api_url=None):
    global _service, _api_url
    if api_url is not None and not valid_api_url(api_url):
        if service is not None and service.configuration.enabled:
            log.warning('Watch turn tools unavailable: unsupported API URL or plaintext listener not loopback')
        api_url = None
    _service, _api_url = service, api_url
    from . import continuation_owners
    continuation_owners.configure(service)


async def run(runner, prompt, *, persistent=False, shell=False, request_text=None, continuation_scope=None, on_launch=None, **kwargs):
    service = _service
    turn = None
    continuation_token = None
    kwargs = dict(kwargs)
    env = dict(kwargs.get('backend_env') or {})
    # Never inherit a human control secret or another turn's credential.
    env.update(SQUID_WATCH_CONTROL_TOKEN=None, SQUID_INTERNAL_WATCH_CONTROL_DIGEST=None,
               SQUID_TURN_TOKEN=None, SQUID_API_URL=None, SQUID_CONTINUE_TOKEN=None)
    instructions = []

    def prepend_notice(name):
        nonlocal prompt
        instruction = launch_instruction(name)
        prompt = instruction['content'] + '\n\n' + prompt
        instructions.insert(0, instruction)

    original_request = prompt if request_text is None else request_text
    inject_notice = not shell and not kwargs.get('resume_session_id') and not original_request.lstrip().startswith('/')
    try:
        if _api_url and not persistent and not shell:
            from . import continuations
            continuation_token = await call(continuations.mint, kwargs.get('msg_id'), continuation_scope,
                                            kwargs.get('response_timeout'))
            if continuation_token:
                env.update(SQUID_API_URL=_api_url, SQUID_CONTINUE_TOKEN=continuation_token)
                if inject_notice and not await call(continuations.sequence_step, kwargs.get('msg_id')):
                    prepend_notice('history')
                from .routes import settings as route_settings
                if inject_notice and route_settings()['enabled']:
                    prepend_notice('routes')
                if inject_notice and not await call(continuations.sequence_step, kwargs.get('msg_id')):
                    prepend_notice('continuations')
        if service is not None and _api_url and not persistent:
            policy = service.configuration.effective_policy(kwargs['topic'])
            if policy.enabled:
                turn = 'turn:' + str(kwargs.get('msg_id')) + ':' + uuid4().hex
                token = await call(WatchControl(service).mint, turn=turn, topic=kwargs['topic'],
                    operations=policy.operations, expires_at=service.clock() + 3600)
                env.update(SQUID_API_URL=_api_url, SQUID_TURN_TOKEN=token)
                if inject_notice:
                    prepend_notice('watch-dispatch' if service.dispatch_enabled and policy.execution_enabled else
                                   'watch-monitoring' if service.monitoring_enabled else 'watch-proposals')
        if continuation_token or turn:
            executable = Path(sys.executable).parent / 'agentsquid'
            if not executable.is_file():
                executable = Path(shutil.which('agentsquid') or executable)
            env['PATH'] = str(executable.parent) + os.pathsep + (env.get('PATH') or os.environ.get('PATH', ''))
        if on_launch is not None:
            from .shared_tools import catalog
            await on_launch(dict(
                instructions=instructions,
                transport='cli',
                operations=catalog() if continuation_token else [],
                watch_operations=sorted(policy.operations) if turn else [],
            ))
        kwargs['backend_env'] = env
        async with aclosing(runner(prompt, **kwargs)) as stream:
            async for chunk in stream:
                yield chunk
    finally:
        if continuation_token:
            await call(continuations.revoke, continuation_token)
        if turn is not None:
            await call(WatchControl(service).revoke_turn, turn)
