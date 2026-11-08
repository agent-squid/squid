"""Source references and launch-time delivery for generated turns.

References are not pins. Only omit source content when its session matches
the session actually being resumed; fresh retries must recompute delivery.
"""
from dataclasses import asdict
from importlib.resources import files
import json

from . import stats_db as db
from .context import load_template


def continuation_instruction(mode):
    if mode not in {'backing_git', 'task_review', 'isolated'}:
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
            # Watch/Flow-owned continuations retain their existing scoped prompt.
            if owner['kind'] == 'human':
                instructions.append(continuation_instruction(row['mode']))
                sources.extend([
                    dict(message_id=request_id, label='original request',
                         session_id=(origin or {}).get('session_id')),
                    dict(message_id=row['parent_id'], label='parent result',
                         session_id=(parent or {}).get('session_id')),
                ])
                # A later step can land in a fresh session. Retain all bounded
                # sequence results, rather than relying on the immediate
                # parent's now-compact input to contain its predecessors.
                from .continuations import _sequence_history
                ancestors = _sequence_history(dict(row))
                for ancestor in ancestors:
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
    sources, instructions = sources_for_turn(msg_id)
    instructions = [dict(asdict(block), delivery='injected') for block in runtime_blocks] + instructions
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
            additions.append(f"{source['label']} (message #{source['message_id']}):\n{content}")
    # Runtime blocks are already rendered by runtime_prompt; append only the
    # continuation policy here, without assembling another handoff prompt.
    additions = [i['content'] for i in instructions if i['source'].startswith('agent/runtime/continuation-')] + additions
    return additions, dict(sources=sources, instructions=instructions,
                           resume_session_id=resume_session_id)


def recorded_context(msg_id):
    with db._connection() as conn:
        rows = conn.execute("SELECT payload FROM run_events WHERE msg_id=? AND event_type='context' ORDER BY seq",
                            (msg_id,)).fetchall()
    attempts = [json.loads(row['payload']) for row in rows]
    if attempts:
        return dict(attempts[-1], attempts=attempts)
    sources, _ = sources_for_turn(msg_id)
    return dict(sources=[dict(s, delivery='reference') for s in sources], instructions=[], attempts=[])
