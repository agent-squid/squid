"""Source references and launch-time delivery for generated turns.

References are not pins. Only omit source content when its session matches
the session actually being resumed; fresh retries must recompute delivery.
"""
from dataclasses import asdict
from importlib.resources import files
import json

from . import stats_db as db
from .context import load_template


CONTINUATION_SOURCE_CHAR_LIMIT = 16_000
# Leave headroom below Linux's 128 KiB argument limit for launch notices.
CONTINUATION_PROMPT_BYTE_LIMIT = 96 * 1024
_TRUNCATION_NOTICE = b'\n[Source truncated]'


def continuation_instruction(mode):
    if mode not in {'backing_git', 'dependency_sync', 'task_review', 'isolated'}:
        raise ValueError('unsupported continuation instruction mode')
    source = f'agent/runtime/continuation-{mode}.md'
    content = files('agent').joinpath(f'runtime/continuation-{mode}.md').read_text(encoding='utf-8')
    return dict(asdict(load_template(content, source).render({})), delivery='injected')


def sources_for_turn(msg_id):
    message = db.get_message(msg_id) if msg_id else None
    if not message:
        return [], []
    sources, instructions = [], []
    if message['source'] == 'continuation':
        with db._connection() as conn:
            row = conn.execute('SELECT * FROM continuations WHERE child_id=?', (msg_id,)).fetchone()
        if row:
            owner = json.loads(row['owner_json'])
            parent = db.get_message(row['parent_id'])
            origin = db.get_message(row['origin_parent_id'] or row['parent_id'])
            request_id = owner.get('request_id') or (origin or {}).get('reply_to')
            host_step = row['mode'] in {'dependency_sync', 'task_publication'}
            external_watch = False
            if owner['kind'] == 'watch':
                request_id = (origin or {}).get('reply_to')
            if owner['kind'] == 'watch' and not host_step:
                from .watch_runs import details, execution_scope
                _, _, grant, _ = details(owner['run_id'])
                source = 'agent/runtime/continuation-watch.md'
                template = files('agent').joinpath('runtime/continuation-watch.md').read_text(encoding='utf-8')
                instructions.append(dict(asdict(load_template(template, source).render(
                    {'instructions': grant['instructions'], 'scope': execution_scope(grant)})), delivery='injected'))
                external_watch = bool(grant.get('external_actions'))
            if not host_step and (not external_watch or row['mode'] != 'isolated'):
                instructions.append(continuation_instruction(row['mode']))
            if origin and request_id == origin['reply_to']:
                request_session = origin.get('session_id')
            else:
                request_session = _delivered_request_session(parent, request_id)
            sources.extend([
                dict(message_id=request_id, label='original request', session_id=request_session),
                dict(message_id=row['parent_id'], label='parent result',
                     session_id=(parent or {}).get('session_id')),
            ])
            from .continuations import _sequence_history
            for ancestor in _sequence_history(dict(row)):
                if ancestor['role'] == 'assistant' and ancestor['id'] != row['parent_id']:
                    sources.append(dict(message_id=ancestor['id'], label='earlier sequence result',
                                        session_id=ancestor.get('session_id')))
    if message.get('flow_run_id'):
        run = db.get_flow_run(message['flow_run_id'])
        step = db.get_flow_step_for_message(msg_id)
        if run and run.get('original_prompt_msg_id'):
            # The original user row itself has no session binding. Its origin
            # assistant does, and is the evidence that this request was seen.
            steps = db.get_flow_steps(run['flow_run_id'])
            origin = next((s for s in steps if s.get('user_msg_id') == run['original_prompt_msg_id']), None)
            origin_message = db.get_message(origin['assistant_msg_id']) if origin and origin.get('assistant_msg_id') else None
            original = db.get_message(run['original_prompt_msg_id'])
            in_request = (not step or (not step['dependencies'] and original
                          and original['content'] and original['content'] in (message.get('prompt') or '')))
            sources.append(dict(message_id=run['original_prompt_msg_id'], label='Flow starting message',
                                session_id=(origin_message or {}).get('session_id'),
                                reference_only=bool(in_request)))
            if step:
                by_id = {s['step_id']: s for s in steps}
                for dependency in step['dependencies']:
                    previous = by_id.get(dependency, {})
                    previous_message = db.get_message(previous['assistant_msg_id']) if previous.get('assistant_msg_id') else None
                    if not previous_message:
                        raise ValueError('Flow source context unavailable')
                    sources.append(dict(message_id=previous_message['id'], label='previous Flow output',
                                        session_id=previous_message.get('session_id')))
    return sorted((s for s in sources if s['message_id']), key=lambda s: s['message_id']), instructions


def launch_context(msg_id, resume_session_id, runtime_blocks, history=()):
    sources, continuation_instructions = sources_for_turn(msg_id)
    instructions = [dict(asdict(block), delivery='injected') for block in runtime_blocks] + continuation_instructions
    additions = []
    history_ids = {message.get('id') for message in history or ()} if not resume_session_id else set()
    for source in sources:
        source['delivery'] = ('reference' if source.get('reference_only') else
                              'session history' if resume_session_id and source['session_id'] == resume_session_id
                              else 'context history' if source['message_id'] in history_ids
                              else 'injected')
        if source['delivery'] == 'injected':
            message = db.get_message(source['message_id'])
            if not message:
                raise ValueError('source context unavailable')
            content = message['content'] or ''
            if message['role'] == 'assistant':
                # Preserve the normal context sanitizer and GitDiff summaries.
                pair = db.get_messages_by_ids([message['id']])
                if pair:
                    content = pair[-1]['content']
            if continuation_instructions and len(content) > CONTINUATION_SOURCE_CHAR_LIMIT:
                content = content[:CONTINUATION_SOURCE_CHAR_LIMIT] + '\n[Source truncated]'
            additions.append(f"{source['label']} (message #{source['message_id']}):\n{content}")
    # Runtime blocks are already rendered by runtime_prompt; append only the
    # continuation policy here, without assembling another handoff prompt.
    additions = [i['content'] for i in continuation_instructions] + additions
    return additions, dict(sources=sources, instructions=instructions,
                           resume_session_id=resume_session_id)


def bound_continuation_context(prompt, additions, delivery, history):
    """Share the launch budget across injected sources and replayed messages."""
    policy_count = sum(i['source'].startswith('agent/runtime/continuation-')
                       for i in delivery['instructions'])
    if not policy_count:
        return '\n\n'.join([*additions, prompt]), history

    from .runners import _build_prompt

    policies, sources = additions[:policy_count], additions[policy_count:]
    replay = history if not delivery['resume_session_id'] and history else []
    empty_replay = [dict(message, content='') for message in replay]
    base = '\n\n'.join([*policies, *([''] * len(sources)), prompt])
    remaining = CONTINUATION_PROMPT_BYTE_LIMIT - len(_build_prompt(base, empty_replay).encode('utf-8'))
    if remaining < 0:
        raise RuntimeError('Continuation prompt exceeds the byte limit before adding history or sources')

    entries = [text.encode('utf-8') for text in sources]
    entries.extend((message.get('content') or '').strip().encode('utf-8') for message in replay)
    if sum(map(len, entries)) <= remaining:
        return '\n\n'.join([*additions, prompt]), history

    # Keep short entries intact and divide the remaining space across long ones.
    bounded = [''] * len(entries)
    for count, index in enumerate(sorted(range(len(entries)), key=lambda index: len(entries[index]))):
        value = entries[index]
        allowance = remaining // (len(entries) - count)
        if len(value) > allowance:
            prefix = value[:max(0, allowance - len(_TRUNCATION_NOTICE))].decode('utf-8', errors='ignore')
            value = prefix.encode('utf-8') + _TRUNCATION_NOTICE[:allowance]
        bounded[index] = value.decode('utf-8')
        remaining -= len(value)

    if replay:
        history = [dict(message, content=content)
                   for message, content in zip(replay, bounded[len(sources):])]
    return '\n\n'.join([*policies, *bounded[:len(sources)], prompt]), history


def _delivered_request_session(parent, request_id):
    if not request_id or not parent or parent['status'] != 'done' or not parent.get('session_id'):
        return None
    attempts = _recorded_attempts(parent['id'])
    if not attempts:
        return None
    # A failed resume may have been followed by a fresh launch. Only the last
    # attempt can establish what the completed parent's session received.
    attempt = attempts[-1]
    session_id = parent['session_id']
    for source in attempt['sources']:
        if source['message_id'] != request_id:
            continue
        delivery = source['delivery']
        if delivery in {'injected', 'context history'}:
            return session_id
        if delivery == 'session history' and attempt['resume_session_id'] == session_id:
            return session_id
        if delivery == 'reference':
            # A routed Flow can carry the request in its own prompt. Verify
            # the complete text because the handoff may have truncated it.
            request = db.get_message(request_id)
            content = (request or {}).get('content')
            if content and content in (parent.get('prompt') or ''):
                return session_id
    return None


def _recorded_attempts(msg_id):
    with db._connection() as conn:
        rows = conn.execute("SELECT payload FROM run_events WHERE msg_id=? AND event_type='context' ORDER BY seq",
                            (msg_id,)).fetchall()
    return [json.loads(row['payload']) for row in rows]


def recorded_context(msg_id):
    attempts = _recorded_attempts(msg_id)
    if attempts:
        return dict(attempts[-1], attempts=attempts)
    sources, _ = sources_for_turn(msg_id)
    return dict(sources=[dict(s, delivery='reference') for s in sources], instructions=[], attempts=[])
