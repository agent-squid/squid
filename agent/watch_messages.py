"""Durable chat anchors for monitors, separate from executable agent turns."""
import json

SCHEMA = ('''CREATE TABLE IF NOT EXISTS watch_messages (
    watch_id TEXT PRIMARY KEY REFERENCES watches(id) ON DELETE CASCADE,
    msg_id INTEGER UNIQUE REFERENCES chat_messages(id) ON DELETE SET NULL,
    last_checked_at REAL
)''',)


def ensure(conn, watch):
    """Create one anchor per watch, atomically with creation; retries reuse it."""
    from .stats_db import _insert_realtime_event
    existing = conn.execute('SELECT msg_id FROM watch_messages WHERE watch_id=?', (watch['id'],)).fetchone()
    if existing is not None and existing['msg_id'] is not None:
        return
    # Acquire the write lock before rechecking: concurrent backfills must share
    # one anchor, including when history deletion left a null message reference.
    conn.execute('INSERT OR IGNORE INTO watch_messages(watch_id) VALUES (?)', (watch['id'],))
    if conn.execute('SELECT msg_id FROM watch_messages WHERE watch_id=?', (watch['id'],)).fetchone()['msg_id'] is not None:
        return
    content = json.dumps(dict(watch_id=watch['id'], target=watch['target'], status=watch['status']))
    msg_id = conn.execute('''INSERT INTO chat_messages(topic,role,source,content,status)
        VALUES (?,'assistant','monitor',?,'done')''', (watch['topic'], content)).lastrowid
    conn.execute('UPDATE watch_messages SET msg_id=? WHERE watch_id=?', (msg_id, watch['id']))
    _insert_realtime_event(conn, 'message.changed', watch['topic'], None, msg_id, None,
                           dict(id=msg_id, role='assistant', source='monitor', status='done'))
