"""
stats_db.py — SQLite for stats, chat history, agents, and topics.
"""
import logging
import sqlite3
import json
import os
from pathlib import Path
from typing import Optional

from .backends import BACKENDS

# Store database in ~/.squid/ so it persists across installs/updates.
# Override with SQUID_DB_PATH env var (e.g. for containers).
_DB_PATH = Path(os.environ.get("SQUID_DB_PATH", Path.home() / ".squid" / "squid.db"))
try:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

_TABLES = [
    """CREATE TABLE IF NOT EXISTS session_stats (
        session_id           TEXT PRIMARY KEY,
        topic                TEXT,
        agent                TEXT,
        backend              TEXT,
        model                TEXT,
        cwd                  TEXT,
        input_tokens         INTEGER,
        output_tokens        INTEGER,
        cache_read_tokens    INTEGER,
        cache_write_tokens   INTEGER,
        history_input_tokens INTEGER DEFAULT 0,
        cost_usd             REAL,
        duration_ms          INTEGER,
        quota_before         REAL,
        quota_after          REAL,
        lookback             INTEGER DEFAULT 0,
        created_at           TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS agents (
        name       TEXT PRIMARY KEY,
        backend    TEXT NOT NULL,
        model      TEXT,
        cwd        TEXT,
        timeout    INTEGER,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS topics (
        topic              TEXT NOT NULL,
        agent              TEXT NOT NULL DEFAULT '',
        sticky_agent       TEXT,
        sticky_adhoc       INTEGER DEFAULT 0,
        last_prompt        TEXT,
        last_adhoc_prompt  TEXT,
        last_at            TEXT,
        last_model         TEXT,
        last_backend       TEXT,
        hidden             INTEGER DEFAULT 0,
        created_at         TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (topic, agent)
    )""",
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        topic       TEXT NOT NULL DEFAULT 'default',
        agent       TEXT,
        session_id  TEXT,
        role        TEXT NOT NULL,
        content     TEXT,
        reply_to    INTEGER REFERENCES chat_messages(id),
        status      TEXT NOT NULL DEFAULT 'pending',
        adhoc       INTEGER DEFAULT 0,
        context     TEXT,
        status_raw  TEXT,
        session_turn_index INTEGER,
        lookback    INTEGER DEFAULT 0,
        quota_delta  REAL,
        quota_before REAL,
        quota_after  REAL,
        created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS topic_sessions (
        topic       TEXT NOT NULL,
        agent       TEXT NOT NULL,
        session_id  TEXT NOT NULL,
        cwd         TEXT NOT NULL,
        backend_fingerprint TEXT,
        created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (topic, agent)
    )""",
    """CREATE TABLE IF NOT EXISTS run_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        msg_id     INTEGER NOT NULL,
        seq        INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload    TEXT,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        UNIQUE(msg_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS git_diff_reverts (
        msg_id      INTEGER NOT NULL,
        repo        TEXT NOT NULL,
        file_path   TEXT NOT NULL,
        reverted_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (msg_id, repo, file_path)
    )""",
    """CREATE TABLE IF NOT EXISTS file_edit_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path   TEXT NOT NULL,
        before      TEXT NOT NULL,
        after       TEXT NOT NULL,
        edited_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_file_edit_history_path ON file_edit_history (file_path, id DESC)",
    """CREATE TABLE IF NOT EXISTS bookmarks (
        msg_id      INTEGER PRIMARY KEY,
        topic       TEXT,
        agent       TEXT,
        content     TEXT,
        saved_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    # FTS5 index — standalone table (not external-content). See ADR-0021.
    # Trigger fires on the status='done' update (final content), not the first
    # partial-content save, so the index always holds the complete response.
    "DROP TRIGGER IF EXISTS messages_fts_sync",
    """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        content, tokenize='unicode61'
    )""",
    """CREATE TRIGGER IF NOT EXISTS messages_fts_sync
       AFTER UPDATE OF content ON chat_messages
       WHEN NEW.role = 'assistant' AND NEW.content IS NOT NULL AND NEW.status = 'done'
       BEGIN
           DELETE FROM messages_fts WHERE rowid = NEW.id;
           INSERT INTO messages_fts(rowid, content) VALUES (NEW.id, NEW.content);
       END""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(
        content, tokenize='unicode61'
    )""",
    """CREATE TRIGGER IF NOT EXISTS prompts_fts_sync
       AFTER INSERT ON chat_messages
       WHEN NEW.role = 'user' AND NEW.content IS NOT NULL
       BEGIN
           INSERT INTO prompts_fts(rowid, content) VALUES (NEW.id, NEW.content);
       END""",
]

# v0.1 baseline — _TABLES above reflects the complete schema.
# Add future schema changes here as new entries after each release.
_MIGRATIONS: list[str] = [
    "ALTER TABLE topic_sessions ADD COLUMN backend_fingerprint TEXT",
    "ALTER TABLE chat_messages ADD COLUMN session_turn_index INTEGER",
    "DROP INDEX IF EXISTS idx_chat_messages_session_turns",
    "ALTER TABLE chat_messages ADD COLUMN lookback INTEGER DEFAULT 0",
    "ALTER TABLE chat_messages ADD COLUMN status_raw TEXT",
    # prompts_fts backfill — safe to re-run; inserts only missing rows
    """INSERT INTO prompts_fts(rowid, content)
       SELECT id, content FROM chat_messages
       WHERE role='user' AND content IS NOT NULL
         AND id NOT IN (SELECT rowid FROM prompts_fts)""",
]

_DATA_MIGRATIONS: list[tuple[str, str]] = [
    ("backfill_session_turn_index", """WITH ranked AS (
           SELECT id, ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY id) AS rn
           FROM chat_messages
           WHERE session_id IS NOT NULL AND role = 'assistant' AND COALESCE(adhoc, 0) = 0
       )
       UPDATE chat_messages
       SET session_turn_index = (SELECT rn FROM ranked WHERE ranked.id = chat_messages.id)
       WHERE session_turn_index IS NULL AND id IN (SELECT id FROM ranked)"""),
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        for ddl in _TABLES:
            conn.execute(ddl)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )"""
        )
        for name, sql in _DATA_MIGRATIONS:
            applied = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
            ).fetchone()
            if applied:
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
        # Seed opencode with its free default model so it works out of the box
        # (must run before the generic loop so the model is set on first insert)
        opencode_backend = BACKENDS.get("opencode")
        if opencode_backend and opencode_backend.available:
            from .config import OPENCODE_DEFAULT_MODEL
            conn.execute(
                "INSERT OR IGNORE INTO agents (name, backend, model) VALUES (?, ?, ?)",
                ("opencode", "opencode", OPENCODE_DEFAULT_MODEL),
            )
        # Seed one default agent per installed enabled CLI (INSERT OR IGNORE — never overwrites user edits)
        for backend, definition in BACKENDS.items():
            if backend == "claude-live":
                continue
            if definition.available:
                conn.execute(
                    "INSERT OR IGNORE INTO agents (name, backend, model) VALUES (?, ?, ?)",
                    (backend, backend, definition.model),
                )
        # Seed haiku as a cost-comparison agent alongside the default claude agent
        claude_backend = BACKENDS.get("claude")
        if claude_backend and claude_backend.available:
            conn.execute(
                "INSERT OR IGNORE INTO agents (name, backend, model) VALUES (?, ?, ?)",
                ("haiku", "claude", "claude-haiku-4-5"),
            )
        # FTS repair: remove entries whose content was indexed mid-stream
        # (partial saves) and now differs from the final stored content.
        conn.execute("""
            DELETE FROM messages_fts
            WHERE rowid IN (
                SELECT mf.rowid FROM messages_fts mf
                JOIN chat_messages m ON m.id = mf.rowid
                WHERE m.role = 'assistant' AND m.status = 'done'
                  AND m.content IS NOT NULL AND m.content != mf.content
            )
        """)
        # Incremental FTS population — adds any rows missing from the index
        # (new since last boot, or just removed above as stale partials).
        conn.execute("""
            INSERT INTO messages_fts(rowid, content)
            SELECT id, content FROM chat_messages
            WHERE role='assistant' AND content IS NOT NULL AND status='done'
              AND id NOT IN (SELECT rowid FROM messages_fts)
        """)
        conn.commit()
    finally:
        conn.close()


# ── agents ────────────────────────────────────────────────────────────────────

def list_agents() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name").fetchall()]


def get_default_agent() -> Optional[dict]:
    """Return the first available agent in fallback order: claude → codex → cursor."""
    with _connect() as conn:
        rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM agents").fetchall()}
    for backend in BACKENDS:
        if backend in rows:
            return rows[backend]
    # Any agent at all
    return next(iter(rows.values()), None) if rows else None


def get_agent(name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_agent(name: str, backend: str, model: Optional[str],
                 cwd: Optional[str] = None) -> bool:
    """Upsert agent config. Returns True if key attributes (backend/model/cwd) changed."""
    with _connect() as conn:
        existing = conn.execute("SELECT backend, model, cwd FROM agents WHERE name = ?", (name,)).fetchone()
        key_changed = existing and (
            existing["backend"] != backend or
            existing["model"] != model or
            existing["cwd"] != cwd
        )
        conn.execute(
            """INSERT INTO agents (name, backend, model, cwd) VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 backend = excluded.backend,
                 model   = excluded.model,
                 cwd     = excluded.cwd""",
            (name, backend, model, cwd),
        )
    return bool(key_changed)


def get_agent_sessions(name: str) -> list[dict]:
    """Return all active topic_sessions for a named agent."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT topic, session_id, cwd FROM topic_sessions WHERE agent = ?", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_agent_sessions(name: str) -> list[str]:
    """Clear all topic_sessions for an agent. Returns list of affected topic names."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT topic FROM topic_sessions WHERE agent = ?", (name,)
        ).fetchall()
        topics = [r["topic"] for r in rows]
        if topics:
            conn.execute("DELETE FROM topic_sessions WHERE agent = ?", (name,))
    return topics


def delete_agent(name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM agents WHERE name = ?", (name,))
        if cur.rowcount:
            conn.execute("DELETE FROM topic_sessions WHERE agent = ?", (name,))
    return cur.rowcount > 0


# ── topics ────────────────────────────────────────────────────────────────────

def get_topics_summary() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT topic AS name, sticky_agent AS agent, sticky_adhoc,
                      last_model, last_backend, last_prompt, last_at
               FROM topics
               WHERE agent = '' AND hidden = 0
               ORDER BY last_at DESC NULLS LAST"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_topics_management_summary(include_hidden: bool = True) -> list[dict]:
    where_hidden = "" if include_hidden else "AND hidden = 0"
    with _connect() as conn:
        topic_rows = conn.execute(
            f"""SELECT topic AS name, sticky_agent AS agent, sticky_adhoc,
                       last_model, last_backend, last_prompt, last_at, hidden
                FROM topics
                WHERE agent = '' {where_hidden}
                ORDER BY last_at DESC NULLS LAST, topic ASC"""
        ).fetchall()
        agent_rows = conn.execute(
            """SELECT topic, agent, last_prompt, last_adhoc_prompt, last_at,
                      last_model, last_backend
               FROM topics
               WHERE agent != ''
               ORDER BY last_at DESC NULLS LAST, agent ASC"""
        ).fetchall()
        # Count completed turns via assistant messages — adhoc flag is only
        # reliably set on assistant rows, not user rows.
        session_turn_rows = conn.execute(
            """SELECT topic, agent, COUNT(*) AS session_turns
               FROM chat_messages
               WHERE role = 'assistant' AND (adhoc = 0 OR adhoc IS NULL)
               GROUP BY topic, agent"""
        ).fetchall()
        adhoc_turn_rows = conn.execute(
            """SELECT topic, agent, COUNT(*) AS adhoc_turns
               FROM chat_messages
               WHERE role = 'assistant' AND adhoc = 1
               GROUP BY topic, agent"""
        ).fetchall()
        agent_turn_rows = conn.execute(
            """SELECT topic, agent, COUNT(*) AS agent_turns
               FROM chat_messages
               WHERE role = 'assistant'
               GROUP BY topic, agent"""
        ).fetchall()
        # Turns in the current live session only (since last /clear or session start)
        live_turn_rows = conn.execute(
            """SELECT cm.topic, cm.agent, COUNT(*) AS live_turns
               FROM chat_messages cm
               JOIN topic_sessions ts
                 ON cm.topic = ts.topic AND cm.agent = ts.agent AND cm.session_id = ts.session_id
               WHERE cm.role = 'assistant' AND (cm.adhoc = 0 OR cm.adhoc IS NULL)
               GROUP BY cm.topic, cm.agent"""
        ).fetchall()

    session_turns_by_key: dict[tuple, int] = {
        (r["topic"], r["agent"]): r["session_turns"] for r in session_turn_rows
    }
    agent_turns_by_key: dict[tuple, int] = {
        (r["topic"], r["agent"]): r["agent_turns"] for r in agent_turn_rows
    }
    adhoc_turns_by_key: dict[tuple, int] = {
        (r["topic"], r["agent"]): r["adhoc_turns"] for r in adhoc_turn_rows
    }
    live_turns_by_key: dict[tuple, int] = {
        (r["topic"], r["agent"]): r["live_turns"] for r in live_turn_rows
    }

    agents_by_topic: dict[str, list[dict]] = {}
    for row in agent_rows:
        item = dict(row)
        topic = item.pop("topic")
        item["session_turns"] = session_turns_by_key.get((topic, item["agent"]), 0)
        item["adhoc_turns"] = adhoc_turns_by_key.get((topic, item["agent"]), 0)
        item["agent_turns"] = agent_turns_by_key.get((topic, item["agent"]), 0)
        item["live_turns"] = live_turns_by_key.get((topic, item["agent"]), 0)
        agents_by_topic.setdefault(topic, []).append(item)

    result = []
    for row in topic_rows:
        item = dict(row)
        item["hidden"] = bool(item.get("hidden"))
        item["agents"] = agents_by_topic.get(item["name"], [])
        item["total_turns"] = sum(a["agent_turns"] for a in item["agents"])
        result.append(item)
    return result


def list_topics() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT topic AS name, sticky_agent AS agent FROM topics WHERE agent = '' ORDER BY topic"
        ).fetchall()]


def get_topic(name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT topic AS name, sticky_agent AS agent, hidden FROM topics WHERE topic = ? AND agent = ''",
            (name,),
        ).fetchone()
    return dict(row) if row else None


def upsert_topic(
    name: str,
    agent: Optional[str] = None,
    last_prompt: Optional[str] = None,
    last_model: Optional[str] = None,
    last_backend: Optional[str] = None,
    adhoc: bool = False,
) -> None:
    now = __import__('time').strftime("%Y-%m-%dT%H:%M:%SZ", __import__('time').gmtime())
    at = now if last_prompt else None
    with _connect() as conn:
        # Topic-level row
        conn.execute(
            """INSERT INTO topics (topic, agent, sticky_agent, sticky_adhoc, last_prompt, last_at, last_model, last_backend)
               VALUES (?, '', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(topic, agent) DO UPDATE SET
                 hidden       = 0,
                 sticky_agent = CASE WHEN excluded.sticky_agent IS NOT NULL THEN excluded.sticky_agent ELSE sticky_agent END,
                 sticky_adhoc = CASE WHEN excluded.sticky_agent IS NOT NULL THEN excluded.sticky_adhoc ELSE sticky_adhoc END,
                 last_prompt  = COALESCE(excluded.last_prompt, last_prompt),
                 last_at      = COALESCE(excluded.last_at, last_at),
                 last_model   = COALESCE(excluded.last_model, last_model),
                 last_backend = COALESCE(excluded.last_backend, last_backend)""",
            (name, agent, 1 if adhoc else 0, last_prompt, at, last_model, last_backend),
        )
        # Agent-level row
        if agent:
            session_prompt = None if adhoc else last_prompt
            adhoc_prompt   = last_prompt if adhoc else None
            conn.execute(
                """INSERT INTO topics (topic, agent, last_prompt, last_adhoc_prompt, last_at, last_model, last_backend)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(topic, agent) DO UPDATE SET
                     last_prompt        = COALESCE(excluded.last_prompt, last_prompt),
                     last_adhoc_prompt  = COALESCE(excluded.last_adhoc_prompt, last_adhoc_prompt),
                     last_at            = COALESCE(excluded.last_at, last_at),
                     last_model         = COALESCE(excluded.last_model, last_model),
                     last_backend       = COALESCE(excluded.last_backend, last_backend)""",
                (name, agent, session_prompt, adhoc_prompt, at, last_model, last_backend),
            )


def set_topic_hidden(name: str, hidden: bool) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE topics SET hidden = ? WHERE topic = ? AND agent = ''",
            (1 if hidden else 0, name),
        )
    return cur.rowcount > 0


def delete_topic(name: str) -> bool:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM chat_messages WHERE topic=? AND role='assistant')",
            (name,),
        )
        conn.execute("DELETE FROM topic_sessions WHERE topic = ?", (name,))
        conn.execute("DELETE FROM session_stats WHERE topic = ?", (name,))
        conn.execute("DELETE FROM chat_messages WHERE topic = ?", (name,))
        cur = conn.execute("DELETE FROM topics WHERE topic = ?", (name,))
    return cur.rowcount > 0


def delete_topic_agent(topic: str, agent: str, adhoc: Optional[bool] = None) -> None:
    with _connect() as conn:
        if adhoc is None:
            conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM chat_messages WHERE topic=? AND agent=? AND role='assistant')",
                (topic, agent),
            )
            conn.execute("DELETE FROM chat_messages WHERE topic=? AND agent=?", (topic, agent))
            conn.execute("DELETE FROM topic_sessions WHERE topic=? AND agent=?", (topic, agent))
            conn.execute("DELETE FROM session_stats WHERE topic=? AND agent=?", (topic, agent))
            conn.execute("DELETE FROM topics WHERE topic=? AND agent=?", (topic, agent))
        elif adhoc:
            conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM chat_messages WHERE topic=? AND agent=? AND adhoc=1 AND role='assistant')",
                (topic, agent),
            )
            conn.execute(
                """DELETE FROM session_stats WHERE session_id IN (
                       SELECT DISTINCT session_id FROM chat_messages
                       WHERE topic=? AND agent=? AND adhoc=1 AND session_id IS NOT NULL
                   )""",
                (topic, agent),
            )
            conn.execute(
                "DELETE FROM chat_messages WHERE topic=? AND agent=? AND adhoc=1",
                (topic, agent),
            )
            conn.execute(
                "UPDATE topics SET last_adhoc_prompt=NULL WHERE topic=? AND agent=?",
                (topic, agent),
            )
            conn.execute(
                "DELETE FROM topics WHERE topic=? AND agent=? AND NOT EXISTS (SELECT 1 FROM chat_messages WHERE topic=? AND agent=?)",
                (topic, agent, topic, agent),
            )
        else:
            conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM chat_messages WHERE topic=? AND agent=? AND (adhoc=0 OR adhoc IS NULL) AND role='assistant')",
                (topic, agent),
            )
            conn.execute(
                "DELETE FROM chat_messages WHERE topic=? AND agent=? AND (adhoc=0 OR adhoc IS NULL)",
                (topic, agent),
            )
            conn.execute("DELETE FROM topic_sessions WHERE topic=? AND agent=?", (topic, agent))
            conn.execute(
                """DELETE FROM session_stats WHERE session_id IN (
                       SELECT DISTINCT session_id FROM chat_messages
                       WHERE topic=? AND agent=? AND (adhoc=0 OR adhoc IS NULL) AND session_id IS NOT NULL
                   )""",
                (topic, agent),
            )
            conn.execute(
                "UPDATE topics SET last_prompt=NULL WHERE topic=? AND agent=?",
                (topic, agent),
            )
            conn.execute(
                "DELETE FROM topics WHERE topic=? AND agent=? AND NOT EXISTS (SELECT 1 FROM chat_messages WHERE topic=? AND agent=?)",
                (topic, agent, topic, agent),
            )


def get_topic_agent_history(topic: str) -> list[dict]:
    """Return agents used in a topic with mode-specific last prompts."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT agent, last_prompt, last_adhoc_prompt
               FROM topics
               WHERE topic = ? AND agent != ''
               ORDER BY last_at DESC""",
            (topic,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── chat messages ─────────────────────────────────────────────────────────────

def insert_user_message(
    topic: str, agent: Optional[str],
    content: str, context_ids: Optional[list[int]] = None,
    mem: bool = False,
    mem_revision: Optional[str] = None,
    lookback: int = 0,
) -> int:
    if context_ids or mem or mem_revision:
        context = {"pins": context_ids or [], "mem": mem}
        if mem_revision:
            context["mem_revision"] = mem_revision
        context_json = json.dumps(context)
    else:
        context_json = None
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (topic, agent, role, content, status, context, lookback)
               VALUES (?, ?, 'user', ?, 'done', ?, ?)""",
            (topic, agent, content, context_json, lookback),
        )
        return cur.lastrowid


def insert_assistant_message(
    topic: str, agent: Optional[str],
    reply_to: int, adhoc: bool = False,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (topic, agent, role, reply_to, status, adhoc)
               VALUES (?, ?, 'assistant', ?, 'pending', ?)""",
            (topic, agent, reply_to, 1 if adhoc else 0),
        )
        return cur.lastrowid


def _ensure_session_turn_index(conn: sqlite3.Connection, msg_id: int, session_id: Optional[str]) -> Optional[int]:
    if not session_id:
        return None
    row = conn.execute(
        """SELECT role, adhoc, session_id, session_turn_index FROM chat_messages
           WHERE id = ?""",
        (msg_id,),
    ).fetchone()
    if not row or row["role"] != "assistant" or row["adhoc"]:
        return None
    if row["session_turn_index"] is not None:
        if row["session_id"] is None:
            conn.execute(
                "UPDATE chat_messages SET session_id = ? WHERE id = ? AND session_id IS NULL",
                (session_id, msg_id),
            )
        return int(row["session_turn_index"])
    prev = conn.execute(
        """SELECT COALESCE(MAX(session_turn_index), 0)
           FROM chat_messages
           WHERE session_id = ? AND role = 'assistant'
             AND COALESCE(adhoc, 0) = 0 AND id != ?""",
        (session_id, msg_id),
    ).fetchone()[0] or 0
    turn_index = int(prev) + 1
    conn.execute(
        """UPDATE chat_messages SET session_id = COALESCE(session_id, ?), session_turn_index = ?
           WHERE id = ? AND session_turn_index IS NULL""",
        (session_id, turn_index, msg_id),
    )
    return turn_index


def ensure_session_turn_index(msg_id: int, session_id: Optional[str]) -> Optional[int]:
    with _connect() as conn:
        return _ensure_session_turn_index(conn, msg_id, session_id)


def update_assistant_message(
    msg_id: int, content: str, session_id: Optional[str], status: str = "done",
    context: Optional[str] = None,
    status_raw: Optional[str] = None,
    only_if_pending: bool = False,
) -> None:
    with _connect() as conn:
        if only_if_pending:
            cur = conn.execute(
                "UPDATE chat_messages SET content=?, session_id=?, status=?, context=?, status_raw=?"
                " WHERE id=? AND status='pending'",
                (content, session_id, status, context, status_raw, msg_id),
            )
            if cur.rowcount:
                _ensure_session_turn_index(conn, msg_id, session_id)
        else:
            conn.execute(
                "UPDATE chat_messages SET content=?, session_id=?, status=?, context=?, status_raw=? WHERE id=?",
                (content, session_id, status, context, status_raw, msg_id),
            )
            if status == "done":
                _ensure_session_turn_index(conn, msg_id, session_id)


def update_message_quota_snapshot(msg_id: int, before: float, after: float) -> None:
    delta = round(after - before, 4)
    with _connect() as conn:
        conn.execute(
            "UPDATE chat_messages SET quota_before=?, quota_after=?, quota_delta=?"
            " WHERE id=? AND role='assistant'",
            (before, after, delta, msg_id),
        )


# ── topic sessions ────────────────────────────────────────────────────────────

def get_topic_agents(topic: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT agent, session_id, cwd, created_at FROM topic_sessions WHERE topic=? ORDER BY agent",
            (topic,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_topic_session(topic: str, agent: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT session_id, cwd, backend_fingerprint FROM topic_sessions WHERE topic=? AND agent=?",
            (topic, agent),
        ).fetchone()
    return dict(row) if row else None


_invalidated_session_ids: set[str] = set()


def set_topic_session(topic: str, agent: str, session_id: str, cwd: Optional[str],
                      backend_fingerprint: Optional[str] = None) -> None:
    if session_id in _invalidated_session_ids:
        _invalidated_session_ids.discard(session_id)
        return
    with _connect() as conn:
        conn.execute(
            """INSERT INTO topic_sessions (topic, agent, session_id, cwd, backend_fingerprint) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(topic, agent) DO UPDATE SET session_id=excluded.session_id,
                 cwd=excluded.cwd, backend_fingerprint=excluded.backend_fingerprint""",
            (topic, agent, session_id, cwd, backend_fingerprint),
        )


def clear_topic_session(topic: str, agent: str) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT session_id FROM topic_sessions WHERE topic=? AND agent=?", (topic, agent)
        ).fetchone()
        if row:
            _invalidated_session_ids.add(row["session_id"])
        conn.execute("DELETE FROM topic_sessions WHERE topic=? AND agent=?", (topic, agent))


def get_session_injected_context(session_id: str) -> dict:
    """Return context injected by user prompts belonging to this backend session."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT u.context
               FROM chat_messages AS u
               JOIN chat_messages AS a ON a.reply_to = u.id
               WHERE a.session_id=? AND u.role='user' AND u.context IS NOT NULL
               ORDER BY u.id""",
            (session_id,),
        ).fetchall()
    ids: list[int] = []
    memory_injected = False
    memory_revision: Optional[str] = None
    for row in rows:
        try:
            v = json.loads(row["context"])
            if isinstance(v, list):
                ids.extend(v)
            elif isinstance(v, dict):
                ids.extend(v.get("pins") or [])
                if v.get("mem"):
                    memory_injected = True
                if v.get("mem_revision"):
                    memory_injected = True
                    memory_revision = v["mem_revision"]
        except Exception:
            pass
    return {
        "injected_ids": list(dict.fromkeys(ids)),  # dedupe preserving order
        "memory_injected": memory_injected,
        "memory_revision": memory_revision,
    }


def get_session_injected_ids(session_id: str) -> list[int]:
    """Return all pin IDs that were injected in user messages belonging to this session."""
    return get_session_injected_context(session_id)["injected_ids"]


# ── context history ────────────────────────────────────────────────────────────

def get_context_history(topic: str, limit: int, agent: Optional[str] = None) -> tuple[list[dict], list[int]]:
    """Return up to `limit` recent non-adhoc exchanges for a topic as context.
    Returns (formatted_history, message_ids) where message_ids are the assistant row IDs included."""
    if limit <= 0:
        return [], []
    _join = """FROM chat_messages a
               JOIN chat_messages u ON u.id = a.reply_to
               WHERE a.topic = ? AND a.role = 'assistant' AND a.status = 'done'
                 AND COALESCE(a.adhoc, 0) = 0
                 AND u.content IS NOT NULL AND a.content IS NOT NULL"""
    sel = "SELECT a.id, u.content AS user_content, a.content AS asst_content "
    agent_clause = " AND a.agent = ?" if agent else ""
    params: tuple = (topic, agent, limit) if agent else (topic, limit)
    with _connect() as conn:
        rows = conn.execute(
            f"{sel}{_join}{agent_clause} ORDER BY a.id DESC LIMIT ?", params,
        ).fetchall()

    result = []
    ids = []
    for row in reversed(rows):
        ids.append(row["id"])
        result.extend([
            {"role": "user",      "content": row["user_content"]},
            {"role": "assistant", "content": row["asst_content"]},
        ])
    return result, ids


def get_messages_by_ids(ids: list[int]) -> list[dict]:
    """Fetch specific assistant messages by ID as context history pairs.
    Returns [user, asst, ...] dicts in ascending ID order. Only done rows with content."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT a.id, u.content AS user_content, a.content AS asst_content
                FROM chat_messages a
                JOIN chat_messages u ON u.id = a.reply_to
                WHERE a.id IN ({placeholders})
                  AND a.role = 'assistant' AND a.status = 'done'
                  AND a.content IS NOT NULL AND u.content IS NOT NULL
                ORDER BY a.id ASC""",
            ids,
        ).fetchall()
    result = []
    for row in rows:
        result.extend([
            {"role": "user",      "content": row["user_content"]},
            {"role": "assistant", "content": row["asst_content"]},
        ])
    return result


def get_topic_messages_for_period(
    topic: str, since_iso: str, until_iso: str,
    agent: Optional[str] = None,
) -> list[dict]:
    """Return [user, asst, ...] pairs for a topic within [since_iso, until_iso).
    Pass agent= to scope to a single agent's turns; omit for all agents."""
    clause = "AND a.agent = ?" if agent else ""
    params: list = [topic, since_iso, until_iso] + ([agent] if agent else [])
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT u.content AS user_content, a.content AS asst_content
               FROM chat_messages a
               JOIN chat_messages u ON u.id = a.reply_to
               WHERE a.topic = ?
                 AND a.role = 'assistant'
                 AND a.status = 'done'
                 AND COALESCE(a.adhoc, 0) = 0
                 AND a.content IS NOT NULL
                 AND a.created_at >= ?
                 AND a.created_at < ?
                 {clause}
               ORDER BY a.id ASC""",
            params,
        ).fetchall()
    result = []
    for r in rows:
        result.append({"role": "user",      "content": r["user_content"] or ""})
        result.append({"role": "assistant", "content": r["asst_content"] or ""})
    return result


def mark_orphaned_pending(before_created_at: Optional[str] = None) -> int:
    with _connect() as conn:
        where = "status='pending' AND role='assistant'"
        params: list[str] = []
        if before_created_at:
            where += " AND created_at < ?"
            params.append(before_created_at)
        rows = conn.execute(
            f"SELECT id, session_id FROM chat_messages WHERE {where}",
            params,
        ).fetchall()
        count = 0
        for row in rows:
            final_text, status_raw = _completed_run_snapshot(conn, row["id"]) or (None, None)
            if final_text:
                conn.execute(
                    "UPDATE chat_messages SET content=?, status='done', status_raw=COALESCE(status_raw, ?) WHERE id=?",
                    (final_text, status_raw, row["id"]),
                )
                _ensure_session_turn_index(conn, row["id"], row["session_id"])
            else:
                conn.execute(
                    "UPDATE chat_messages SET content='', status='error' WHERE id=?",
                    (row["id"],),
                )
            count += 1
        return count


def get_message(msg_id: int) -> Optional[dict]:
    stat_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "history_input_tokens", "cost_usd", "duration_ms", "lookback",
                 "quota_before", "quota_after", "quota_delta",
                 "msg_quota_before", "msg_quota_after", "backend", "model", "cwd"}
    with _connect() as conn:
        row = conn.execute(
            """SELECT m.id, m.role, m.topic, m.agent,
                      m.content, m.status, m.adhoc, m.session_id,
                      m.context, m.status_raw, m.created_at AS timestamp, m.reply_to,
                      m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                      u.content AS prompt, u.context AS prompt_context,
                      m.session_turn_index AS session_turn_count,
                      s.input_tokens, s.output_tokens, s.cache_read_tokens,
                      s.cache_write_tokens, s.history_input_tokens,
                      s.cost_usd, s.duration_ms, s.lookback,
                      s.quota_before, s.quota_after, s.backend, s.model, s.cwd
               FROM chat_messages m
               LEFT JOIN chat_messages u ON m.reply_to = u.id
               LEFT JOIN session_stats s ON m.session_id = s.session_id
               WHERE m.id=?""",
            (msg_id,)
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    stats = {k: result.pop(k) for k in stat_keys}
    if result.get("session_id") and any(v is not None for v in stats.values()):
        stats["session_id"] = result["session_id"]
        result["stats"] = stats
    return result


def get_messages_flat(topic: Optional[str] = None, agent: Optional[str] = None,
                      adhoc: Optional[bool] = None, offset: int = 0, limit: int = 20) -> dict:
    where = "WHERE m.role = 'assistant'"
    params: list = []
    if topic:
        where += " AND m.topic = ?"
        params.append(topic)
    if agent:
        where += " AND m.agent = ?"
        params.append(agent)
    if adhoc is not None:
        where += " AND COALESCE(m.adhoc, 0) = ?"
        params.append(1 if adhoc else 0)

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM chat_messages m {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                       m.content, m.status, m.adhoc, m.session_id,
                       m.context, m.status_raw, m.created_at AS timestamp, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context,
                       m.session_turn_index AS session_turn_count,
                       s.input_tokens, s.output_tokens, s.cache_read_tokens,
                       s.cache_write_tokens, s.history_input_tokens,
                       s.cost_usd, s.duration_ms, s.lookback,
                       s.quota_before, s.quota_after, s.backend, s.model, s.cwd
                FROM chat_messages m
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                LEFT JOIN session_stats s ON m.session_id = s.session_id
                {where}
                ORDER BY m.id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

    stat_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "history_input_tokens", "cost_usd", "duration_ms", "lookback",
                 "quota_before", "quota_after", "quota_delta",
                 "msg_quota_before", "msg_quota_after", "backend", "model", "cwd"}
    items = []
    for r in rows:
        row = dict(r)
        stats = {k: row.pop(k) for k in stat_keys}
        if row.get("session_id") and any(v is not None for v in stats.values()):
            stats["session_id"] = row["session_id"]
            row["stats"] = stats
        items.append(row)

    return {
        "items": items,
        "total": total,
        "has_more": (offset + limit) < total,
    }


def get_history_items_by_ids(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    stat_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "history_input_tokens", "cost_usd", "duration_ms", "lookback",
                 "quota_before", "quota_after", "quota_delta",
                 "msg_quota_before", "msg_quota_after", "backend", "model", "cwd"}
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                       m.content, m.status, m.adhoc, m.session_id,
                       m.context, m.status_raw, m.created_at AS timestamp, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context,
                       m.session_turn_index AS session_turn_count,
                       s.input_tokens, s.output_tokens, s.cache_read_tokens,
                       s.cache_write_tokens, s.history_input_tokens,
                       s.cost_usd, s.duration_ms, s.lookback,
                       s.quota_before, s.quota_after, s.backend, s.model, s.cwd
                FROM chat_messages m
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                LEFT JOIN session_stats s ON m.session_id = s.session_id
                WHERE m.id IN ({placeholders})
                ORDER BY m.id ASC""",
            ids,
        ).fetchall()
    items = []
    for r in rows:
        row = dict(r)
        stats = {k: row.pop(k) for k in stat_keys}
        if row.get("session_id") and any(v is not None for v in stats.values()):
            stats["session_id"] = row["session_id"]
            row["stats"] = stats
        items.append(row)
    return items


def _build_fts_match(q: str) -> str:
    tokens = [t.replace('"', '') for t in q.strip().split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return ''
    return ' AND '.join(f'"{t}"' for t in tokens)


def search_messages(q: str, topic: Optional[str] = None, agent: Optional[str] = None,
                    adhoc: Optional[bool] = None, limit: int = 100,
                    bookmarked: bool = False) -> dict:
    terms = _build_fts_match(q)
    if not terms:
        return {"items": []}

    where_parts = [
        "m.role = 'assistant'",
        "m.status = 'done'",
        "m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)",
    ]
    params: list = [terms]

    if topic:
        where_parts.append("m.topic = ?")
        params.append(topic)
    if agent:
        where_parts.append("m.agent = ?")
        params.append(agent)
    if adhoc is not None:
        where_parts.append("COALESCE(m.adhoc, 0) = ?")
        params.append(1 if adhoc else 0)

    where = "WHERE " + " AND ".join(where_parts)
    bookmark_join = "JOIN bookmarks b ON b.msg_id = m.id" if bookmarked else ""

    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                       m.content, m.status, m.adhoc, m.session_id,
                       m.context, m.status_raw, m.created_at AS timestamp, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context,
                       m.session_turn_index AS session_turn_count,
                       s.input_tokens, s.output_tokens, s.cache_read_tokens,
                       s.cache_write_tokens, s.history_input_tokens,
                       s.cost_usd, s.duration_ms, s.lookback,
                       s.quota_before, s.quota_after, s.backend, s.model, s.cwd
                FROM chat_messages m
                {bookmark_join}
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                LEFT JOIN session_stats s ON m.session_id = s.session_id
                {where}
                ORDER BY m.id DESC LIMIT ?""",
            params + [limit],
        ).fetchall()

    stat_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "history_input_tokens", "cost_usd", "duration_ms", "lookback",
                 "quota_before", "quota_after", "quota_delta",
                 "msg_quota_before", "msg_quota_after", "backend", "model", "cwd"}
    items = []
    for r in rows:
        row = dict(r)
        stats = {k: row.pop(k) for k in stat_keys}
        if row.get("session_id") and any(v is not None for v in stats.values()):
            stats["session_id"] = row["session_id"]
            row["stats"] = stats
        items.append(row)

    return {"items": items}


def search_prompts(q: str, topic: Optional[str] = None, agent: Optional[str] = None,
                   adhoc: Optional[bool] = None, limit: int = 100,
                   bookmarked: bool = False) -> dict:
    """Search user prompts via prompts_fts. Returns assistant reply items (same shape as
    search_messages) so the frontend can render them with appendPromptOnlyHistoryItem."""
    terms = _build_fts_match(q)
    if not terms:
        return {"items": []}

    where_parts = [
        "m.role = 'assistant'",
        "m.status = 'done'",
        "m.reply_to IN (SELECT rowid FROM prompts_fts WHERE prompts_fts MATCH ?)",
    ]
    params: list = [terms]

    if topic:
        where_parts.append("m.topic = ?")
        params.append(topic)
    if agent:
        where_parts.append("m.agent = ?")
        params.append(agent)
    if adhoc is not None:
        where_parts.append("COALESCE(m.adhoc, 0) = ?")
        params.append(1 if adhoc else 0)

    where = "WHERE " + " AND ".join(where_parts)
    bookmark_join = "JOIN bookmarks b ON b.msg_id = m.id" if bookmarked else ""

    stat_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "history_input_tokens", "cost_usd", "duration_ms", "lookback",
                 "quota_before", "quota_after", "quota_delta",
                 "msg_quota_before", "msg_quota_after", "backend", "model", "cwd"}

    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                       m.content, m.status, m.adhoc, m.session_id,
                       m.context, m.status_raw, m.created_at AS timestamp, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context,
                       m.session_turn_index AS session_turn_count,
                       s.input_tokens, s.output_tokens, s.cache_read_tokens,
                       s.cache_write_tokens, s.history_input_tokens,
                       s.cost_usd, s.duration_ms, s.lookback,
                       s.quota_before, s.quota_after, s.backend, s.model, s.cwd
                FROM chat_messages m
                {bookmark_join}
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                LEFT JOIN session_stats s ON m.session_id = s.session_id
                {where}
                ORDER BY m.id DESC LIMIT ?""",
            params + [limit],
        ).fetchall()

    items = []
    for r in rows:
        row = dict(r)
        stats = {k: row.pop(k) for k in stat_keys}
        if row.get("session_id") and any(v is not None for v in stats.values()):
            stats["session_id"] = row["session_id"]
            row["stats"] = stats
        items.append(row)

    return {"items": items}


def get_recent_prompts(limit: int = 50) -> list:
    with _connect() as conn:
        rows = conn.execute(
            """WITH routed_prompts AS (
                   SELECT u.id,
                          TRIM(u.content) AS content,
                          u.topic,
                          u.agent,
                          COALESCE((
                              SELECT a.adhoc
                              FROM chat_messages a
                              WHERE a.reply_to = u.id AND a.role = 'assistant'
                              ORDER BY a.id ASC
                              LIMIT 1
                          ), u.adhoc, 0) AS adhoc,
                          u.lookback
                   FROM chat_messages u
                   WHERE u.role = 'user'
                     AND u.content IS NOT NULL
                     AND TRIM(u.content) != ''
               ), latest_unique AS (
                   SELECT MAX(id) AS id
                   FROM routed_prompts
                   GROUP BY content, topic, agent, adhoc
               )
               SELECT p.content, p.topic, p.agent, p.adhoc, p.lookback
               FROM routed_prompts p
               JOIN latest_unique latest ON latest.id = p.id
               ORDER BY p.id DESC
               LIMIT ?""",
            [limit],
        ).fetchall()

    results = []
    seen: set = set()
    for r in rows:
        row = dict(r)
        content = (row['content'] or '').strip()
        if not content:
            continue
        topic = row.get('topic') or ''
        agent = row.get('agent') or ''
        adhoc = bool(row.get('adhoc'))
        prefix = ''
        if topic and (topic != 'default' or agent):
            prefix = f'#{topic}'
            if agent:
                prefix += f'@{agent}'
            if adhoc:
                prefix += '!'
            prefix += ' '
        full = prefix + content
        if full not in seen:
            seen.add(full)
            results.append(full)
    return results


# ── session stats ─────────────────────────────────────────────────────────────

def save_stats(
    session_id: str,
    stats: dict,
    topic: Optional[str] = None,
    agent: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    lookback: int = 0,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO session_stats
                   (session_id, topic, agent, backend, model, cwd,
                    input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, history_input_tokens,
                    cost_usd, duration_ms, lookback, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(session_id) DO UPDATE SET
                   topic                = COALESCE(excluded.topic, topic),
                   agent                = COALESCE(excluded.agent, agent),
                   backend              = COALESCE(excluded.backend, backend),
                   model                = COALESCE(excluded.model, model),
                   cwd                  = COALESCE(excluded.cwd, cwd),
                   input_tokens         = excluded.input_tokens,
                   output_tokens        = excluded.output_tokens,
                   cache_read_tokens    = excluded.cache_read_tokens,
                   cache_write_tokens   = excluded.cache_write_tokens,
                   history_input_tokens = excluded.history_input_tokens,
                   cost_usd             = excluded.cost_usd,
                   duration_ms          = excluded.duration_ms,
                   lookback             = excluded.lookback""",
            (
                session_id, topic, agent, backend, model, cwd,
                stats.get("input_tokens", 0), stats.get("output_tokens", 0),
                stats.get("cache_read_tokens", 0), stats.get("cache_write_tokens", 0),
                stats.get("history_input_tokens", 0),
                stats.get("cost_usd"), stats.get("duration_ms"),
                lookback,
            ),
        )


def save_quota_delta(session_id: str, before: float, after: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE session_stats SET quota_before=?, quota_after=? WHERE session_id=?",
            (before, after, session_id),
        )


def get_stats(session_id: str) -> Optional[dict]:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_stats WHERE session_id=?", (session_id,)
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None


def _stats_cutoff(days: int) -> Optional[str]:
    if not days:
        return None
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def get_aggregated_stats(
    period: str = "daily",
    days: int = 30,
    agent: str = "",
    topic: str = "",
    adhoc: str = "all",
    tz_offset_minutes: int = 0,
) -> list:
    # Shift UTC timestamps to local time before bucketing so day boundaries
    # reflect the user's clock, not UTC midnight.
    # getTimezoneOffset() returns minutes to subtract from local to get UTC,
    # so negating it gives the offset to add to UTC to get local.
    tz_shift = f"{-tz_offset_minutes} minutes"
    ss_bucket = (
        f"strftime('%Y-%m-%d %H:00', datetime(ss_inner.created_at, '{tz_shift}'))"
        if period == "hourly"
        else f"strftime('%Y-%m-%d', datetime(ss_inner.created_at, '{tz_shift}'))"
    )
    cm_bucket = (
        f"strftime('%Y-%m-%d %H:00', datetime(cm.created_at, '{tz_shift}'))"
        if period == "hourly"
        else f"strftime('%Y-%m-%d', datetime(cm.created_at, '{tz_shift}'))"
    )
    limit = (days * (24 if period == "hourly" else 1) + 1) if days else 5000
    cutoff = _stats_cutoff(days)

    ss_clauses: list[str] = ["ss_inner.created_at IS NOT NULL"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("ss_inner.created_at >= ?")
        ss_params.append(cutoff)
    if agent:
        ss_clauses.append("COALESCE(ss_inner.agent, ss_inner.backend) = ?")
        ss_params.append(agent)
    if topic:
        ss_clauses.append("ss_inner.topic = ?")
        ss_params.append(topic)

    cm_clauses: list[str] = ["cm.role = 'assistant'"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("cm.created_at >= ?")
        cm_params.append(cutoff)
    if agent:
        cm_clauses.append("cm.agent = ?")
        cm_params.append(agent)
    if topic:
        cm_clauses.append("cm.topic = ?")
        cm_params.append(topic)

    if adhoc == "session":
        s_expr = "COUNT(CASE WHEN cm.adhoc = 0 OR cm.adhoc IS NULL THEN 1 END)"
        a_expr = "0"
    elif adhoc == "adhoc":
        s_expr = "0"
        a_expr = "COUNT(CASE WHEN cm.adhoc = 1 THEN 1 END)"
    else:
        s_expr = "COUNT(CASE WHEN cm.adhoc = 0 OR cm.adhoc IS NULL THEN 1 END)"
        a_expr = "COUNT(CASE WHEN cm.adhoc = 1 THEN 1 END)"

    ss_where = " AND ".join(ss_clauses)
    cm_where = " AND ".join(cm_clauses)

    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""SELECT
                        ss.period,
                        ss.sessions,
                        ss.input_tokens,
                        ss.new_input_tokens,
                        ss.output_tokens,
                        ss.cache_read_tokens,
                        ss.cache_write_tokens,
                        ss.cost_usd,
                        ss.quota_delta,
                        COALESCE(cm.session_turns, 0) AS session_turns,
                        COALESCE(cm.adhoc_turns, 0) AS adhoc_turns,
                        COALESCE(cm.session_turns, 0) + COALESCE(cm.adhoc_turns, 0) AS total_turns
                    FROM (
                        SELECT
                            {ss_bucket} AS period,
                            COUNT(*) AS sessions,
                            SUM(input_tokens) AS input_tokens,
                            SUM(input_tokens - COALESCE(history_input_tokens, 0)) AS new_input_tokens,
                            SUM(output_tokens) AS output_tokens,
                            SUM(cache_read_tokens) AS cache_read_tokens,
                            SUM(cache_write_tokens) AS cache_write_tokens,
                            SUM(cost_usd) AS cost_usd,
                            SUM(CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL
                                     THEN quota_after - quota_before ELSE NULL END) AS quota_delta
                        FROM session_stats ss_inner
                        WHERE {ss_where}
                        GROUP BY period ORDER BY period DESC LIMIT ?
                    ) ss
                    LEFT JOIN (
                        SELECT {cm_bucket} AS period,
                               {s_expr} AS session_turns,
                               {a_expr} AS adhoc_turns
                        FROM chat_messages cm
                        WHERE {cm_where}
                        GROUP BY period
                    ) cm ON ss.period = cm.period
                    ORDER BY ss.period DESC""",
                (*ss_params, limit, *cm_params),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def get_stats_by_agent(
    days: int = 30, agent: str = "", topic: str = "", adhoc: str = "all"
) -> list:
    cutoff = _stats_cutoff(days)

    ss_clauses: list[str] = ["1=1"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("created_at >= ?")
        ss_params.append(cutoff)
    if topic:
        ss_clauses.append("topic = ?")
        ss_params.append(topic)
    if agent:
        ss_clauses.append("COALESCE(agent, backend) = ?")
        ss_params.append(agent)

    cm_clauses: list[str] = ["role = 'assistant'"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("created_at >= ?")
        cm_params.append(cutoff)
    if topic:
        cm_clauses.append("topic = ?")
        cm_params.append(topic)
    if agent:
        cm_clauses.append("agent = ?")
        cm_params.append(agent)

    if adhoc == "session":
        turn_expr = "COUNT(CASE WHEN adhoc = 0 OR adhoc IS NULL THEN 1 END)"
    elif adhoc == "adhoc":
        turn_expr = "COUNT(CASE WHEN adhoc = 1 THEN 1 END)"
    else:
        turn_expr = "COUNT(*)"

    ss_where = " AND ".join(ss_clauses)
    cm_where = " AND ".join(cm_clauses)

    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""SELECT ss.agent,
                          ss.sessions,
                          ss.input_tokens,
                          ss.output_tokens,
                          ss.cache_read_tokens,
                          ss.cache_write_tokens,
                          ss.cost_usd,
                          ss.quota_delta,
                          COALESCE(cm.turns, 0) AS total_turns
                   FROM (
                       SELECT COALESCE(agent, backend, 'unknown') AS agent,
                              COUNT(*) AS sessions,
                              SUM(input_tokens) AS input_tokens,
                              SUM(output_tokens) AS output_tokens,
                              SUM(cache_read_tokens) AS cache_read_tokens,
                              SUM(cache_write_tokens) AS cache_write_tokens,
                              SUM(cost_usd) AS cost_usd,
                              SUM(CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL
                                       THEN quota_after - quota_before ELSE NULL END) AS quota_delta
                       FROM session_stats
                       WHERE {ss_where}
                       GROUP BY agent
                   ) ss
                   LEFT JOIN (
                       SELECT agent, {turn_expr} AS turns
                       FROM chat_messages
                       WHERE {cm_where}
                       GROUP BY agent
                   ) cm ON ss.agent = cm.agent
                   ORDER BY ss.sessions DESC""",
                (*ss_params, *cm_params),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def get_stats_by_topic(
    days: int = 30, agent: str = "", topic: str = "", adhoc: str = "all"
) -> list:
    cutoff = _stats_cutoff(days)

    ss_clauses: list[str] = ["topic IS NOT NULL"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("created_at >= ?")
        ss_params.append(cutoff)
    if agent:
        ss_clauses.append("COALESCE(agent, backend) = ?")
        ss_params.append(agent)
    if topic:
        ss_clauses.append("topic = ?")
        ss_params.append(topic)

    cm_clauses: list[str] = ["role = 'assistant'"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("created_at >= ?")
        cm_params.append(cutoff)
    if agent:
        cm_clauses.append("agent = ?")
        cm_params.append(agent)
    if topic:
        cm_clauses.append("topic = ?")
        cm_params.append(topic)

    if adhoc == "session":
        turn_expr = "COUNT(CASE WHEN adhoc = 0 OR adhoc IS NULL THEN 1 END)"
    elif adhoc == "adhoc":
        turn_expr = "COUNT(CASE WHEN adhoc = 1 THEN 1 END)"
    else:
        turn_expr = "COUNT(*)"

    ss_where = " AND ".join(ss_clauses)
    cm_where = " AND ".join(cm_clauses)

    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""SELECT ss.topic,
                          ss.sessions,
                          ss.input_tokens,
                          ss.output_tokens,
                          ss.cache_read_tokens,
                          ss.cost_usd,
                          ss.quota_delta,
                          COALESCE(cm.turns, 0) AS total_turns
                   FROM (
                       SELECT topic,
                              COUNT(*) AS sessions,
                              SUM(input_tokens) AS input_tokens,
                              SUM(output_tokens) AS output_tokens,
                              SUM(cache_read_tokens) AS cache_read_tokens,
                              SUM(cost_usd) AS cost_usd,
                              SUM(CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL
                                       THEN quota_after - quota_before ELSE NULL END) AS quota_delta
                       FROM session_stats
                       WHERE {ss_where}
                       GROUP BY topic
                   ) ss
                   LEFT JOIN (
                       SELECT topic, {turn_expr} AS turns
                       FROM chat_messages
                       WHERE {cm_where}
                       GROUP BY topic
                   ) cm ON ss.topic = cm.topic
                   ORDER BY ss.sessions DESC""",
                (*ss_params, *cm_params),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def get_stats_filter_options() -> dict:
    try:
        with _connect() as conn:
            agents = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT COALESCE(agent, backend) FROM session_stats"
                    " WHERE agent IS NOT NULL OR backend IS NOT NULL ORDER BY 1"
                ).fetchall() if r[0]
            ]
            topics = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT topic FROM session_stats WHERE topic IS NOT NULL ORDER BY 1"
                ).fetchall()
            ]
        return {"agents": agents, "topics": topics}
    except sqlite3.OperationalError:
        return {"agents": [], "topics": []}


# ── run events ─────────────────────────────────────────────────────────────────

# ── git diff reverts ───────────────────────────────────────────────────────────

def record_git_diff_revert(msg_id: int, repo: str, file_paths: list[str]) -> None:
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO git_diff_reverts (msg_id, repo, file_path) VALUES (?, ?, ?)",
            [(msg_id, repo, fp) for fp in file_paths],
        )


def get_diff_revert_eligibility(msg_id: int, repo: str) -> dict[str, str]:
    """Return {file_path: 'revertable'|'conflicting'|'reverted'} for a GitDiff event.

    A file is revertable when no non-reverted GitDiff with a higher msg_id
    in the same topic and repo has also touched that file.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT topic, context FROM chat_messages WHERE id = ? AND role = 'assistant'",
            (msg_id,),
        ).fetchone()
        if not row or not row['context']:
            return {}

        topic = row['topic']
        try:
            tools = json.loads(row['context'])
        except Exception:
            return {}

        this_diff = next(
            (t for t in tools if t.get('name') == 'GitDiff' and t.get('repo') == repo),
            None,
        )
        if not this_diff:
            return {}

        this_files = {f['path'] for f in this_diff.get('files', [])}
        if not this_files:
            return {}

        already_reverted = {
            r['file_path'] for r in conn.execute(
                "SELECT file_path FROM git_diff_reverts WHERE msg_id = ? AND repo = ?",
                (msg_id, repo),
            ).fetchall()
        }

        later_rows = conn.execute(
            """SELECT id, context FROM chat_messages
               WHERE topic = ? AND role = 'assistant' AND id > ? AND context IS NOT NULL""",
            (topic, msg_id),
        ).fetchall()

        later_ids = [r['id'] for r in later_rows]
        later_reverted_map: dict[int, set[str]] = {}
        if later_ids:
            placeholders = ','.join('?' * len(later_ids))
            for r in conn.execute(
                f"SELECT msg_id, file_path FROM git_diff_reverts"
                f" WHERE msg_id IN ({placeholders}) AND repo = ?",
                [*later_ids, repo],
            ).fetchall():
                later_reverted_map.setdefault(r['msg_id'], set()).add(r['file_path'])

    later_touched: set[str] = set()
    for later_row in later_rows:
        try:
            later_tools = json.loads(later_row['context'])
        except Exception:
            continue
        for t in later_tools:
            if t.get('name') == 'GitDiff' and t.get('repo') == repo:
                lr = later_reverted_map.get(later_row['id'], set())
                for f in t.get('files', []):
                    fpath = f['path']
                    if fpath not in lr:
                        later_touched.add(fpath)

    return {
        fpath: (
            'reverted' if fpath in already_reverted
            else 'conflicting' if fpath in later_touched
            else 'revertable'
        )
        for fpath in this_files
    }


def get_message_gitdiff(msg_id: int, repo: str) -> Optional[dict]:
    """Return the GitDiff tool event for a given message and repo, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT context FROM chat_messages WHERE id = ? AND role = 'assistant'",
            (msg_id,),
        ).fetchone()
    if not row or not row['context']:
        return None
    try:
        tools = json.loads(row['context'])
    except Exception:
        return None
    return next(
        (t for t in tools if t.get('name') == 'GitDiff' and t.get('repo') == repo),
        None,
    )


def save_file_edit(file_path: str, before: str, after: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO file_edit_history (file_path, before, after) VALUES (?, ?, ?)",
            (file_path, before, after),
        )
        return cur.lastrowid


def get_file_edit_history(file_path: str, limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, file_path, edited_at FROM file_edit_history WHERE file_path = ? ORDER BY id DESC LIMIT ?",
            (file_path, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_file_edit_by_id(edit_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, file_path, before, after, edited_at FROM file_edit_history WHERE id = ?",
            (edit_id,),
        ).fetchone()
    return dict(row) if row else None


def insert_run_event(msg_id: int, seq: int, event_type: str, payload: Optional[str]) -> None:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO run_events (msg_id, seq, event_type, payload) VALUES (?, ?, ?, ?)",
            (msg_id, seq, event_type, payload),
        )
        if cur.rowcount == 0:
            logging.getLogger("squid").warning(
                "run_event seq collision: msg_id=%s seq=%s event_type=%s — event silently dropped",
                msg_id, seq, event_type,
            )


def _completed_run_snapshot(conn: sqlite3.Connection, msg_id: int) -> Optional[tuple[str, Optional[str]]]:
    rows = conn.execute(
        "SELECT event_type, payload FROM run_events WHERE msg_id=? ORDER BY seq",
        (msg_id,),
    ).fetchall()
    if not any(row["event_type"] == "done" for row in rows):
        return None
    text = "".join(row["payload"] or "" for row in rows if row["event_type"] == "text")
    status_raw = "".join(row["payload"] or "" for row in rows if row["event_type"] == "status")
    return text, status_raw or None


def _completed_run_text(conn: sqlite3.Connection, msg_id: int) -> Optional[str]:
    snapshot = _completed_run_snapshot(conn, msg_id)
    return snapshot[0] if snapshot else None


def get_completed_run_text(msg_id: int) -> Optional[str]:
    with _connect() as conn:
        return _completed_run_text(conn, msg_id)


def get_completed_run_status_raw(msg_id: int) -> Optional[str]:
    with _connect() as conn:
        snapshot = _completed_run_snapshot(conn, msg_id)
        return snapshot[1] if snapshot else None


def get_run_events(msg_id: int, after_seq: int = -1) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT seq, event_type, payload FROM run_events WHERE msg_id=? AND seq>? ORDER BY seq",
            (msg_id, after_seq),
        ).fetchall()
    return [dict(r) for r in rows]


# ── bookmarks ─────────────────────────────────────────────────────────────────

def get_bookmarks() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT msg_id AS id, topic, agent, content, saved_at FROM bookmarks ORDER BY saved_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_bookmark(msg_id: int, topic: Optional[str], agent: Optional[str], content: Optional[str]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bookmarks (msg_id, topic, agent, content) VALUES (?, ?, ?, ?)",
            (msg_id, topic, agent, content),
        )


def remove_bookmark(msg_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM bookmarks WHERE msg_id = ?", (msg_id,))
