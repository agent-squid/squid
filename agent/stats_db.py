"""
stats_db.py — SQLite for stats, chat history, aliases, and topics.
"""
import sqlite3
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent / "squid.db"

_TABLES = [
    """CREATE TABLE IF NOT EXISTS topic_sessions (
        topic       TEXT NOT NULL,
        alias       TEXT NOT NULL,
        session_id  TEXT NOT NULL,
        cwd         TEXT NOT NULL,
        created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (topic, alias)
    )""",
    """CREATE TABLE IF NOT EXISTS session_context_log (
        topic       TEXT NOT NULL,
        alias       TEXT NOT NULL,
        msg_id      INTEGER NOT NULL REFERENCES chat_messages(id),
        injected_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (topic, alias, msg_id)
    )""",
    """CREATE TABLE IF NOT EXISTS session_stats (
        session_id           TEXT PRIMARY KEY,
        topic                TEXT,
        alias                TEXT,
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
        created_at           TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS aliases (
        name       TEXT PRIMARY KEY,
        backend    TEXT NOT NULL DEFAULT 'auto',
        model      TEXT,
        cwd        TEXT,
        timeout    INTEGER,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS topics (
        name       TEXT PRIMARY KEY,
        alias      TEXT,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        topic      TEXT NOT NULL DEFAULT 'default',
        alias      TEXT,
        backend    TEXT,
        model      TEXT,
        session_id TEXT,
        role       TEXT NOT NULL,
        content    TEXT,
        reply_to   INTEGER REFERENCES chat_messages(id),
        status     TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
    "ALTER TABLE session_stats ADD COLUMN alias TEXT",
    "ALTER TABLE session_stats ADD COLUMN backend TEXT",
    "ALTER TABLE chat_messages ADD COLUMN pinned INTEGER DEFAULT 0",
    "ALTER TABLE session_stats ADD COLUMN lookback INTEGER DEFAULT 0",
    "ALTER TABLE session_stats ADD COLUMN pin_count INTEGER DEFAULT 0",
    "ALTER TABLE chat_messages ADD COLUMN tools TEXT",
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
                pass  # column already exists
        conn.commit()
    finally:
        conn.close()


# ── aliases ───────────────────────────────────────────────────────────────────

def list_aliases() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM aliases ORDER BY name").fetchall()]


def get_alias(name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM aliases WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_alias(name: str, backend: str, model: Optional[str],
                 cwd: Optional[str] = None, timeout: Optional[int] = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO aliases (name, backend, model, cwd, timeout) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 backend = excluded.backend,
                 model   = excluded.model,
                 cwd     = excluded.cwd,
                 timeout = excluded.timeout""",
            (name, backend, model, cwd, timeout),
        )


def delete_alias(name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM aliases WHERE name = ?", (name,))
        if cur.rowcount:
            conn.execute("DELETE FROM session_context_log WHERE alias = ?", (name,))
            conn.execute("DELETE FROM topic_sessions WHERE alias = ?", (name,))
    return cur.rowcount > 0


# ── topics ────────────────────────────────────────────────────────────────────

def get_topics_summary() -> list[dict]:
    """Return all topics with their most recent prompt and timestamp."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT t.name, t.alias,
                      a.model AS last_model,
                      a.backend AS last_backend,
                      u.content  AS last_prompt,
                      a.created_at AS last_at
               FROM topics t
               LEFT JOIN chat_messages a ON a.id = (
                   SELECT id FROM chat_messages
                   WHERE topic = t.name AND role = 'assistant' AND status IN ('done', 'error')
                   ORDER BY id DESC LIMIT 1
               )
               LEFT JOIN chat_messages u ON u.id = a.reply_to
               ORDER BY COALESCE(a.created_at, t.created_at) DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def list_topics() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM topics ORDER BY name").fetchall()]


def get_topic(name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM topics WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_topic(name: str, alias: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO topics (name, alias) VALUES (?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 alias = CASE WHEN excluded.alias IS NOT NULL THEN excluded.alias ELSE alias END""",
            (name, alias),
        )


def delete_topic(name: str) -> bool:
    """Remove topic and all its active session state. Chat history is preserved."""
    with _connect() as conn:
        conn.execute("DELETE FROM session_context_log WHERE topic = ?", (name,))
        conn.execute("DELETE FROM topic_sessions WHERE topic = ?", (name,))
        cur = conn.execute("DELETE FROM topics WHERE name = ?", (name,))
    return cur.rowcount > 0


# ── chat messages ─────────────────────────────────────────────────────────────

def insert_user_message(
    topic: str, alias: Optional[str], backend: Optional[str], model: Optional[str], content: str
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (topic, alias, backend, model, role, content, status)
               VALUES (?, ?, ?, ?, 'user', ?, 'done')""",
            (topic, alias, backend, model, content),
        )
        return cur.lastrowid


def insert_assistant_message(
    topic: str, alias: Optional[str], backend: Optional[str], model: Optional[str],
    reply_to: int, adhoc: bool = False,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (topic, alias, backend, model, role, reply_to, status, adhoc)
               VALUES (?, ?, ?, ?, 'assistant', ?, 'pending', ?)""",
            (topic, alias, backend, model, reply_to, 1 if adhoc else 0),
        )
        return cur.lastrowid


def update_assistant_message(
    msg_id: int, content: str, session_id: Optional[str], status: str = "done",
    tools: Optional[str] = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE chat_messages SET content=?, session_id=?, status=?, tools=? WHERE id=?",
            (content, session_id, status, tools, msg_id),
        )


def pin_message(msg_id: int, pinned: int) -> None:
    """pinned: 1 = always include, 0 = default, -1 = always exclude."""
    with _connect() as conn:
        conn.execute("UPDATE chat_messages SET pinned=? WHERE id=?", (pinned, msg_id))


def reset_pins() -> None:
    with _connect() as conn:
        conn.execute("UPDATE chat_messages SET pinned=0 WHERE pinned != 0")


def reset_topic_pins(topic: str) -> None:
    """Clear pinned=1 for a topic after they've been consumed as adhoc context."""
    with _connect() as conn:
        conn.execute("UPDATE chat_messages SET pinned=0 WHERE topic=? AND pinned=1", (topic,))


# ── topic sessions ────────────────────────────────────────────────────────────

def get_topic_aliases(topic: str) -> list[dict]:
    """Return all aliases that have active sessions for a topic."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT alias, session_id, cwd, created_at FROM topic_sessions WHERE topic=? ORDER BY alias",
            (topic,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_topic_session(topic: str, alias: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT session_id, cwd FROM topic_sessions WHERE topic=? AND alias=?",
            (topic, alias),
        ).fetchone()
    return dict(row) if row else None


def set_topic_session(topic: str, alias: str, session_id: str, cwd: Optional[str]) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO topic_sessions (topic, alias, session_id, cwd) VALUES (?, ?, ?, ?)
               ON CONFLICT(topic, alias) DO UPDATE SET session_id=excluded.session_id, cwd=excluded.cwd""",
            (topic, alias, session_id, cwd),
        )


def clear_topic_session(topic: str, alias: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM topic_sessions WHERE topic=? AND alias=?", (topic, alias))
        conn.execute("DELETE FROM session_context_log WHERE topic=? AND alias=?", (topic, alias))


def get_pending_injections(topic: str, alias: str,
                           exclude_session_id: Optional[str] = None) -> list[dict]:
    """Return pinned messages not yet injected into this (topic, alias) session.
    exclude_session_id: skip messages produced in this session — already in --resume context."""
    excl = " AND (m.session_id IS NULL OR m.session_id != ?)" if exclude_session_id else ""
    params: tuple = (topic, alias)
    if exclude_session_id:
        params += (exclude_session_id,)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.content, m.alias AS source_alias, m.adhoc
               FROM chat_messages m
               WHERE m.pinned = 1
                 AND m.content IS NOT NULL
                 AND m.id NOT IN (
                     SELECT msg_id FROM session_context_log
                     WHERE topic=? AND alias=?
                 ){excl}
               ORDER BY m.id ASC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def mark_injected(topic: str, alias: str, msg_ids: list[int]) -> None:
    if not msg_ids:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO session_context_log (topic, alias, msg_id) VALUES (?, ?, ?)",
            [(topic, alias, mid) for mid in msg_ids],
        )


def get_session_context_log(topic: str, alias: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT msg_id, injected_at FROM session_context_log WHERE topic=? AND alias=? ORDER BY injected_at ASC",
            (topic, alias),
        ).fetchall()
    return [dict(r) for r in rows]


# ── context history ────────────────────────────────────────────────────────────

def get_context_history(topic: str, limit: int, alias: Optional[str] = None) -> tuple[list[dict], int]:
    """Return curated context exchanges for a topic.
    Pinned messages are always included topic-wide (no alias filter), including adhoc ones.
    If limit > 0, also includes up to `limit` most-recent non-adhoc exchanges,
    scoped to alias when provided. Recent rows are deduped against pinned."""
    _join = """FROM chat_messages a
               JOIN chat_messages u ON u.id = a.reply_to
               WHERE a.topic = ? AND a.role = 'assistant' AND a.status = 'done'
                 AND u.content IS NOT NULL AND a.content IS NOT NULL"""
    # Pinned can be adhoc or non-adhoc — user explicitly chose them
    pinned_base = _join
    # Recent lookback only pulls non-adhoc session turns
    recent_base = _join + " AND COALESCE(a.adhoc, 0) = 0"
    sel = ("SELECT a.id, u.content AS user_content, a.content AS asst_content,"
           " a.model AS asst_model, a.backend AS asst_backend ")
    with _connect() as conn:
        pinned = conn.execute(
            f"{sel}{pinned_base} AND a.pinned = 1 ORDER BY a.id ASC", (topic,),
        ).fetchall()
        if limit > 0:
            alias_clause = " AND a.alias = ?" if alias else ""
            recent_params: tuple = (topic, alias, limit) if alias else (topic, limit)
            recent = conn.execute(
                f"{sel}{recent_base}{alias_clause} ORDER BY a.id DESC LIMIT ?", recent_params,
            ).fetchall()
        else:
            recent = []

    def _pair(row):
        return [
            {"role": "user",      "content": row["user_content"]},
            {"role": "assistant", "content": row["asst_content"],
             "model": row["asst_model"], "backend": row["asst_backend"]},
        ]

    pinned_ids = {r["id"] for r in pinned}
    result = []
    for row in pinned:
        result.extend(_pair(row))
    for row in reversed(recent):
        if row["id"] not in pinned_ids:
            result.extend(_pair(row))
    # Count directly — INNER JOIN above may miss pins with NULL reply_to or user content
    with _connect() as conn:
        pin_count = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE topic=? AND role='assistant' AND pinned=1",
            (topic,),
        ).fetchone()[0]
    return result, pin_count


def mark_orphaned_pending() -> int:
    """On server start, any pending message from a previous run will never complete."""
    with _connect() as conn:
        cur = conn.execute("UPDATE chat_messages SET status='error' WHERE status='pending'")
        return cur.rowcount


def get_message(msg_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, status, content FROM chat_messages WHERE id=?", (msg_id,)
        ).fetchone()
    return dict(row) if row else None


def get_messages_flat(topic: Optional[str] = None, alias: Optional[str] = None, offset: int = 0, limit: int = 20) -> dict:
    """Return individual messages (user + assistant) in chronological order for the /history endpoint."""
    where = "WHERE 1=1"
    params: list = []
    if topic:
        where += " AND m.topic = ?"
        params.append(topic)
    if alias:
        where += " AND m.alias = ?"
        params.append(alias)

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM chat_messages m {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.alias, m.backend, m.model,
                       m.content, m.status, m.pinned, m.adhoc, m.session_id,
                       m.tools, m.created_at AS timestamp, m.reply_to,
                       u.content AS prompt
                FROM chat_messages m
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                {where}
                ORDER BY m.id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "has_more": (offset + limit) < total,
    }


# ── session stats ─────────────────────────────────────────────────────────────

def save_stats(
    session_id: str,
    stats: dict,
    topic: Optional[str] = None,
    alias: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    lookback: int = 0,
    pin_count: int = 0,
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO session_stats
                   (session_id, topic, alias, backend, model, cwd,
                    input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, history_input_tokens,
                    cost_usd, duration_ms, lookback, pin_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   topic                = COALESCE(excluded.topic, topic),
                   alias                = COALESCE(excluded.alias, alias),
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
                   lookback             = excluded.lookback,
                   pin_count            = excluded.pin_count""",
            (
                session_id, topic, alias, backend, model, cwd,
                stats.get("input_tokens", 0), stats.get("output_tokens", 0),
                stats.get("cache_read_tokens", 0), stats.get("cache_write_tokens", 0),
                stats.get("history_input_tokens", 0),
                stats.get("cost_usd"), stats.get("duration_ms"),
                lookback, pin_count,
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


def get_stats_by_model() -> list:
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT COALESCE(model, backend, 'unknown') AS model,
                          COUNT(*) AS sessions,
                          SUM(input_tokens) AS input_tokens,
                          SUM(output_tokens) AS output_tokens,
                          SUM(cache_read_tokens) AS cache_read_tokens,
                          SUM(cache_write_tokens) AS cache_write_tokens,
                          SUM(cost_usd) AS cost_usd
                   FROM session_stats
                   GROUP BY model ORDER BY sessions DESC"""
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
                          SUM(cost_usd) AS cost_usd
                   FROM session_stats WHERE topic IS NOT NULL
                   GROUP BY topic ORDER BY sessions DESC"""
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
