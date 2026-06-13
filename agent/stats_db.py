"""
stats_db.py — SQLite for stats, chat history, agents, and topics.
"""
import sqlite3
import json
from pathlib import Path
from typing import Optional

from .config import CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH

_BACKEND_FALLBACK_ORDER = ["claude", "codex", "cursor", "antigravity", "copilot"]
_BACKEND_PATHS = {
    "claude":       CLAUDE_PATH,
    "codex":        CODEX_PATH,
    "cursor":       CURSOR_PATH,
    "antigravity":  AGY_PATH,
    "copilot":      COPILOT_PATH,
}

_DB_PATH = Path(__file__).parent.parent / "squid.db"

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
]

_MIGRATIONS = [
    "ALTER TABLE chat_messages ADD COLUMN adhoc INTEGER DEFAULT 0",
    "ALTER TABLE aliases ADD COLUMN timeout INTEGER",
    "ALTER TABLE session_stats ADD COLUMN model TEXT",
    "ALTER TABLE session_stats ADD COLUMN cwd TEXT",
    "ALTER TABLE session_stats ADD COLUMN created_at TEXT",
    "ALTER TABLE session_stats ADD COLUMN history_input_tokens INTEGER DEFAULT 0",
    "ALTER TABLE session_stats ADD COLUMN quota_before REAL",
    "ALTER TABLE session_stats ADD COLUMN quota_after REAL",
    "ALTER TABLE session_stats ADD COLUMN topic TEXT",
    "ALTER TABLE session_stats ADD COLUMN backend TEXT",
    "ALTER TABLE chat_messages ADD COLUMN pinned INTEGER DEFAULT 0",
    "ALTER TABLE session_stats ADD COLUMN lookback INTEGER DEFAULT 0",
    "ALTER TABLE session_stats ADD COLUMN pin_count INTEGER DEFAULT 0",
    "ALTER TABLE chat_messages ADD COLUMN tools TEXT",
    # alias → agent column renames (2026-05-28)
    "ALTER TABLE topics RENAME COLUMN alias TO agent",
    "ALTER TABLE chat_messages RENAME COLUMN alias TO agent",
    "ALTER TABLE topic_sessions RENAME COLUMN alias TO agent",
    "ALTER TABLE session_context_log RENAME COLUMN alias TO agent",
    "ALTER TABLE session_stats RENAME COLUMN alias TO agent",
    # tools → context, drop pinned/pin_count (2026-05-28)
    "ALTER TABLE chat_messages RENAME COLUMN tools TO context",
    "ALTER TABLE chat_messages DROP COLUMN pinned",
    "ALTER TABLE session_stats DROP COLUMN pin_count",
    # drop backend/model from chat_messages — ground truth is in session_stats (2026-05-28)
    "ALTER TABLE chat_messages DROP COLUMN backend",
    "ALTER TABLE chat_messages DROP COLUMN model",
    # hide support for topics (2026-05-28)
    "ALTER TABLE topics ADD COLUMN hidden INTEGER DEFAULT 0",
    # sticky_adhoc tracks whether the sticky agent was set via adhoc (!) mode (2026-06-11)
    "ALTER TABLE topics ADD COLUMN sticky_adhoc INTEGER DEFAULT 0",
    # last_adhoc_prompt stores the most recent adhoc prompt per agent row (2026-06-11)
    "ALTER TABLE topics ADD COLUMN last_adhoc_prompt TEXT",
    # per-message quota delta (2026-06-09)
    "ALTER TABLE chat_messages ADD COLUMN quota_delta REAL",
    # per-message raw quota snapshot (2026-06-09)
    "ALTER TABLE chat_messages ADD COLUMN quota_before REAL",
    "ALTER TABLE chat_messages ADD COLUMN quota_after REAL",
    # git diff revert tracking (2026-06-12)
    """CREATE TABLE IF NOT EXISTS git_diff_reverts (
        msg_id      INTEGER NOT NULL,
        repo        TEXT NOT NULL,
        file_path   TEXT NOT NULL,
        reverted_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (msg_id, repo, file_path)
    )""",
    # denormalize last_model/last_backend into topics to avoid JOIN in get_topics_summary (2026-05-28)
    "ALTER TABLE topics ADD COLUMN last_model TEXT",
    "ALTER TABLE topics ADD COLUMN last_backend TEXT",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        # One-time table rename: aliases → agents
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "aliases" in tables and "agents" not in tables:
            conn.execute("ALTER TABLE aliases RENAME TO agents")
        # Drop legacy table no longer needed
        conn.execute("DROP TABLE IF EXISTS session_context_log")

        # Recreate topics table with composite PK (topic, agent) if still on old schema
        cols = {r[1] for r in conn.execute("PRAGMA table_info(topics)").fetchall()} if "topics" in tables else set()
        if "topics" in tables and "last_at" not in cols:
            conn.execute("""CREATE TABLE topics_new (
                topic        TEXT NOT NULL,
                agent        TEXT NOT NULL DEFAULT '',
                sticky_agent TEXT,
                last_prompt  TEXT,
                last_at      TEXT,
                hidden       INTEGER DEFAULT 0,
                created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                PRIMARY KEY (topic, agent)
            )""")
            conn.execute("""INSERT INTO topics_new (topic, agent, sticky_agent, hidden, created_at)
                           SELECT name, '', agent, COALESCE(hidden, 0), created_at FROM topics""")
            conn.execute("DROP TABLE topics")
            conn.execute("ALTER TABLE topics_new RENAME TO topics")

        for ddl in _TABLES:
            conn.execute(ddl)
        for sql in _MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        # Seed one default agent per installed CLI (INSERT OR IGNORE — never overwrites user edits)
        for backend, path in _BACKEND_PATHS.items():
            if path:
                conn.execute(
                    "INSERT OR IGNORE INTO agents (name, backend) VALUES (?, ?)",
                    (backend, backend),
                )
        # Backfill last_prompt/last_at for topic rows that predate the denormalization
        conn.execute("""
            UPDATE topics SET
                last_prompt = (
                    SELECT content FROM chat_messages
                    WHERE topic = topics.topic AND role = 'user' AND content IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                ),
                last_at = (
                    SELECT created_at FROM chat_messages
                    WHERE topic = topics.topic AND role = 'user' AND content IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                )
            WHERE last_prompt IS NULL
        """)
        # Backfill last_backend/last_model from session_stats for topics that predate the denormalization
        conn.execute("""
            UPDATE topics SET
                last_backend = (
                    SELECT backend FROM session_stats
                    WHERE topic = topics.topic AND backend IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1
                ),
                last_model = (
                    SELECT model FROM session_stats
                    WHERE topic = topics.topic AND model IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1
                )
            WHERE last_backend IS NULL
        """)
        conn.commit()
    finally:
        conn.close()


# ── agents ────────────────────────────────────────────────────────────────────

def list_agents() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name").fetchall()]


def get_default_agent() -> Optional[dict]:
    """Return the first available agent in fallback order: claude → codex → cursor → antigravity → copilot."""
    with _connect() as conn:
        rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM agents").fetchall()}
    for backend in _BACKEND_FALLBACK_ORDER:
        if backend in rows:
            return rows[backend]
    # Any agent at all
    return next(iter(rows.values()), None) if rows else None


def get_agent(name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_agent(name: str, backend: str, model: Optional[str],
                 cwd: Optional[str] = None, timeout: Optional[int] = None) -> bool:
    """Upsert agent config. Returns True if key attributes (backend/model/cwd) changed."""
    with _connect() as conn:
        existing = conn.execute("SELECT backend, model, cwd FROM agents WHERE name = ?", (name,)).fetchone()
        key_changed = existing and (
            existing["backend"] != backend or
            existing["model"] != model or
            existing["cwd"] != cwd
        )
        conn.execute(
            """INSERT INTO agents (name, backend, model, cwd, timeout) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 backend = excluded.backend,
                 model   = excluded.model,
                 cwd     = excluded.cwd,
                 timeout = excluded.timeout""",
            (name, backend, model, cwd, timeout),
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

    agents_by_topic: dict[str, list[dict]] = {}
    for row in agent_rows:
        item = dict(row)
        topic = item.pop("topic")
        agents_by_topic.setdefault(topic, []).append(item)

    result = []
    for row in topic_rows:
        item = dict(row)
        item["hidden"] = bool(item.get("hidden"))
        item["agents"] = agents_by_topic.get(item["name"], [])
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


def hide_topic(name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("UPDATE topics SET hidden = 1 WHERE topic = ? AND agent = ''", (name,))
    return cur.rowcount > 0


def set_topic_hidden(name: str, hidden: bool) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE topics SET hidden = ? WHERE topic = ? AND agent = ''",
            (1 if hidden else 0, name),
        )
    return cur.rowcount > 0


def delete_topic(name: str) -> bool:
    with _connect() as conn:
        conn.execute("DELETE FROM topic_sessions WHERE topic = ?", (name,))
        conn.execute("DELETE FROM session_stats WHERE topic = ?", (name,))
        conn.execute("DELETE FROM chat_messages WHERE topic = ?", (name,))
        cur = conn.execute("DELETE FROM topics WHERE topic = ?", (name,))
    return cur.rowcount > 0


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
) -> int:
    context_json = json.dumps({"pins": context_ids or [], "mem": mem}) if (context_ids or mem) else None
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (topic, agent, role, content, status, context)
               VALUES (?, ?, 'user', ?, 'done', ?)""",
            (topic, agent, content, context_json),
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


def update_assistant_message(
    msg_id: int, content: str, session_id: Optional[str], status: str = "done",
    context: Optional[str] = None,
    only_if_pending: bool = False,
) -> None:
    with _connect() as conn:
        if only_if_pending:
            conn.execute(
                "UPDATE chat_messages SET content=?, session_id=?, status=?, context=?"
                " WHERE id=? AND status='pending'",
                (content, session_id, status, context, msg_id),
            )
        else:
            conn.execute(
                "UPDATE chat_messages SET content=?, session_id=?, status=?, context=? WHERE id=?",
                (content, session_id, status, context, msg_id),
            )


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
            "SELECT session_id, cwd FROM topic_sessions WHERE topic=? AND agent=?",
            (topic, agent),
        ).fetchone()
    return dict(row) if row else None


_invalidated_session_ids: set[str] = set()


def set_topic_session(topic: str, agent: str, session_id: str, cwd: Optional[str]) -> None:
    if session_id in _invalidated_session_ids:
        _invalidated_session_ids.discard(session_id)
        return
    with _connect() as conn:
        conn.execute(
            """INSERT INTO topic_sessions (topic, agent, session_id, cwd) VALUES (?, ?, ?, ?)
               ON CONFLICT(topic, agent) DO UPDATE SET session_id=excluded.session_id, cwd=excluded.cwd""",
            (topic, agent, session_id, cwd),
        )


def clear_topic_session(topic: str, agent: str) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT session_id FROM topic_sessions WHERE topic=? AND agent=?", (topic, agent)
        ).fetchone()
        if row:
            _invalidated_session_ids.add(row["session_id"])
        conn.execute("DELETE FROM topic_sessions WHERE topic=? AND agent=?", (topic, agent))


def get_session_injected_ids(session_id: str) -> list[int]:
    """Return all pin IDs that were injected in user messages belonging to this session."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT context FROM chat_messages WHERE session_id=? AND role='user' AND context IS NOT NULL",
            (session_id,),
        ).fetchall()
    ids: list[int] = []
    for row in rows:
        try:
            v = json.loads(row["context"])
            if isinstance(v, list):
                ids.extend(v)
            elif isinstance(v, dict):
                ids.extend(v.get("pins") or [])
        except Exception:
            pass
    return list(dict.fromkeys(ids))  # dedupe preserving order


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


def mark_orphaned_pending() -> int:
    with _connect() as conn:
        cur = conn.execute("UPDATE chat_messages SET status='error' WHERE status='pending'")
        return cur.rowcount


def get_message(msg_id: int) -> Optional[dict]:
    stat_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "history_input_tokens", "cost_usd", "duration_ms", "lookback",
                 "quota_before", "quota_after", "quota_delta",
                 "msg_quota_before", "msg_quota_after", "backend", "model", "cwd"}
    with _connect() as conn:
        row = conn.execute(
            """SELECT m.id, m.role, m.topic, m.agent,
                      m.content, m.status, m.adhoc, m.session_id,
                      m.context, m.created_at AS timestamp, m.reply_to,
                      m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                      u.content AS prompt, u.context AS prompt_context,
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
                       m.context, m.created_at AS timestamp, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context,
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


def get_aggregated_stats(period: str = "daily") -> list:
    bucket = (
        "strftime('%Y-%m-%d %H:00', created_at)"
        if period == "hourly"
        else "strftime('%Y-%m-%d', created_at)"
    )
    limit = 48 if period == "hourly" else 30
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""SELECT
                        {bucket} AS period,
                        COUNT(*) AS sessions,
                        SUM(input_tokens) AS input_tokens,
                        SUM(input_tokens - COALESCE(history_input_tokens, 0)) AS new_input_tokens,
                        SUM(output_tokens) AS output_tokens,
                        SUM(cache_read_tokens) AS cache_read_tokens,
                        SUM(cache_write_tokens) AS cache_write_tokens,
                        SUM(cost_usd) AS cost_usd,
                        SUM(CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL
                                 THEN quota_after - quota_before ELSE NULL END) AS quota_delta
                    FROM session_stats WHERE created_at IS NOT NULL
                    GROUP BY period ORDER BY period DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def get_stats_by_agent() -> list:
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT COALESCE(agent, backend, 'unknown') AS agent,
                          COUNT(*) AS sessions,
                          SUM(input_tokens) AS input_tokens,
                          SUM(output_tokens) AS output_tokens,
                          SUM(cache_read_tokens) AS cache_read_tokens,
                          SUM(cache_write_tokens) AS cache_write_tokens,
                          SUM(cost_usd) AS cost_usd
                   FROM session_stats
                   GROUP BY agent ORDER BY sessions DESC"""
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def get_stats_by_topic() -> list:
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT topic, COUNT(*) AS sessions,
                          SUM(input_tokens) AS input_tokens,
                          SUM(output_tokens) AS output_tokens,
                          SUM(cache_read_tokens) AS cache_read_tokens,
                          SUM(cost_usd) AS cost_usd
                   FROM session_stats WHERE topic IS NOT NULL
                   GROUP BY topic ORDER BY sessions DESC"""
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


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


def insert_run_event(msg_id: int, seq: int, event_type: str, payload: Optional[str]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO run_events (msg_id, seq, event_type, payload) VALUES (?, ?, ?, ?)",
            (msg_id, seq, event_type, payload),
        )


def get_run_events(msg_id: int, after_seq: int = -1) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT seq, event_type, payload FROM run_events WHERE msg_id=? AND seq>? ORDER BY seq",
            (msg_id, after_seq),
        ).fetchall()
    return [dict(r) for r in rows]
