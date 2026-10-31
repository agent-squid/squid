"""Launch-scoped watch credentials. Persistent harnesses remain unsupported."""
import logging
import os
from pathlib import Path
import shutil
import sys
from contextlib import aclosing
from uuid import uuid4

from .watch_async import call
from .watch_urls import valid_api_url
from .watch_service import WatchControl

log = logging.getLogger(__name__)

_service = None
_api_url = None


def configure(service, api_url=None):
    global _service, _api_url
    if api_url is not None and not valid_api_url(api_url):
        if service is not None and service.configuration.enabled:
            log.warning('Watch turn tools unavailable: unsupported API URL or plaintext listener not loopback')
        api_url = None
    _service, _api_url = service, api_url


async def run(runner, prompt, *, persistent=False, shell=False, request_text=None, **kwargs):
    service = _service
    turn = None
    kwargs = dict(kwargs)
    env = dict(kwargs.get('backend_env') or {})
    # Never inherit a human control secret or another turn's credential.
    env.update(SQUID_WATCH_CONTROL_TOKEN=None, SQUID_INTERNAL_WATCH_CONTROL_DIGEST=None,
               SQUID_TURN_TOKEN=None, SQUID_API_URL=None)
    try:
        if service is not None and _api_url and not persistent:
            policy = service.configuration.effective_policy(kwargs['topic'])
            if policy.enabled:
                turn = 'turn:' + str(kwargs.get('msg_id')) + ':' + uuid4().hex
                token = await call(WatchControl(service).mint, turn=turn, topic=kwargs['topic'],
                    operations=policy.operations, expires_at=service.clock() + 3600)
                env.update(SQUID_API_URL=_api_url, SQUID_TURN_TOKEN=token)
                executable = Path(sys.executable).parent / 'agentsquid'
                if not executable.is_file():
                    executable = Path(shutil.which('agentsquid') or executable)
                env['PATH'] = str(executable.parent) + os.pathsep + (env.get('PATH') or os.environ.get('PATH', ''))
                original_request = prompt if request_text is None else request_text
                if not shell and not kwargs.get('resume_session_id') and not original_request.lstrip().startswith('/'):
                    prompt = ('Squid shared tools: `agentsquid tools --help`; '
                              '`agentsquid watch --help`. Watch registration currently saves '
                              'proposals only; persistent monitoring is not enabled.\n\n' + prompt)
        kwargs['backend_env'] = env
        async with aclosing(runner(prompt, **kwargs)) as stream:
            async for chunk in stream:
                yield chunk
    finally:
        if turn is not None:
            await call(WatchControl(service).revoke_turn, turn)
