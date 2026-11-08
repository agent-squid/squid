"""Bounded read-only history for the issuing turn's topic or explicit subtree."""
import re

from . import routes, stats_db as db


def source(row):
    return dict(message_id=row['id'], topic=row['topic'], agent=row['agent'],
                timestamp=row['timestamp'], role=row['role'],
                jump_command=f"/jump {row['id']}")


def invoke(token, action, body):
    with db._connection() as conn:
        parent, _ = routes.principal(conn, token)
        topic = parent['topic']
    # Never let a missing topic become the database API's unfiltered search.
    if not topic:
        raise PermissionError()
    if not isinstance(body, dict):
        raise ValueError('invalid_history_request')
    scope = body.get('scope', 'exact')
    if scope not in ('exact', 'subtree'):
        raise ValueError('invalid_history_request')
    subtree = scope == 'subtree'
    if action == 'search':
        query, limit, target = body.get('query'), body.get('limit', 10), body.get('target', 'responses')
        if (set(body) - {'query', 'limit', 'target', 'scope'} or not isinstance(query, str)
                or not query.strip() or len(query) > 512 or '\x00' in query or type(limit) is not int
                or not 1 <= limit <= 20 or target not in ('responses', 'prompts')):
            raise ValueError('invalid_history_request')
        search = db.search_prompts if target == 'prompts' else db.search_messages
        rows = search(query, topic=topic, limit=limit, topic_subtree=subtree)['items']
        items = []
        for row in rows:
            if target == 'prompts':
                # A reply link alone is not authority to read its prompt. Check
                # the live source independently, including its role and topic.
                with db._connection() as conn:
                    prompt = conn.execute(
                        "SELECT content FROM chat_messages WHERE id=? AND topic=? AND role='user'",
                        (row['reply_to'], row['topic'])).fetchone()
                if prompt is None:
                    continue
                content = prompt['content'] or ''
            else:
                content = row['content'] or ''
            # Keep excerpts near the first query term, without returning run metadata.
            # Strip quotes as the existing FTS query builder does. Regex match
            # offsets refer to the original text even when Unicode casing expands.
            terms = [term.replace('"', '') for term in query.split()]
            term = next((term for term in terms if term), '')
            match = re.search(re.escape(term), content, re.IGNORECASE) if term else None
            start = max(0, match.start() - 160) if match else 0
            items.append(dict(**source(row), excerpt=content[start:start + 800],
                              excerpt_target=target, prompt_message_id=row['reply_to']))
        return dict(items=items, topic=topic, scope=scope, backend='fts5', order='newest_first',
                    historical_context=True)
    if action == 'fetch':
        identifier, offset = body.get('message_id'), body.get('offset', 0)
        if (set(body) - {'message_id', 'offset', 'scope'} or type(identifier) is not int
                or not 1 <= identifier <= 2**63 - 1 or type(offset) is not int or offset < 0):
            raise ValueError('invalid_history_request')
        params = []
        clause = db._topic_filter_clause(topic, subtree, params)
        with db._connection() as conn:
            row = conn.execute(
                f'''SELECT m.id, m.topic, m.agent, m.role, m.content, m.status,
                           m.reply_to, m.created_at AS timestamp
                    FROM chat_messages m WHERE m.id=? AND {clause}''',
                [identifier] + params).fetchone()
        if (not row or row['role'] not in ('assistant', 'user')
                or (row['role'] == 'assistant' and row['status'] != 'done')):
            raise ValueError('history_not_found')
        content = row['content'] or ''
        end = offset + 8000
        return dict(**source(row), content=content[offset:end], offset=offset,
                    next_offset=end if end < len(content) else None,
                    reply_to=row['reply_to'], scope=scope, historical_context=True)
    raise ValueError('invalid_history_request')
