"""
stats_db.py — SQLite for stats, chat history, aliases, and topics.
"""
import sqlite3
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent / "squid.db"

_TABLES = [
    """CREATE TABLE IF NOT EXISTS session_stats (
        session_id           TEXT PRIMARY KEY,
        topic                TEXT,
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
    "ALTER TABLE session_stats ADD COLUMN created_at TEXT",
    "ALTER TABLE session_stats ADD COLUMN history_input_tokens INTEGER DEFAULT 0",
    "ALTER TABLE session_stats ADD COLUMN quota_before REAL",
    "ALTER TABLE session_stats ADD COLUMN quota_after REAL",
    "ALTER TABLE session_stats ADD COLUMN topic TEXT",
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


def upsert_alias(name: str, backend: str, model: Optional[str], cwd: Optional[str]) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO aliases (name, backend, model, cwd) VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 backend = excluded.backend,
                 model   = excluded.model,
                 cwd     = excluded.cwd""",
            (name, backend, model, cwd),
        )


def delete_alias(name: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM aliases WHERE name = ?", (name,))
    return cur.rowcount > 0


# ── topics ────────────────────────────────────────────────────────────────────

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
    topic: str, alias: Optional[str], backend: Optional[str], model: Optional[str], reply_to: int
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (topic, alias, backend, model, role, reply_to, status)
               VALUES (?, ?, ?, ?, 'assistant', ?, 'pending')""",
            (topic, alias, backend, model, reply_to),
        )
        return cur.lastrowid


def update_assistant_message(
    msg_id: int, content: str, session_id: Optional[str], status: str = "done"
) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE chat_messages SET content=?, session_id=?, status=? WHERE id=?",
            (content, session_id, status, msg_id),
        )


def get_context_history(topic: str, limit: int) -> list[dict]:
    """Return the last `limit` complete exchanges as [{role, content}] for prompt injection."""
    if limit <= 0:
        return []
    with _connect() as conn:
        rows = conn.execute(
            """SELECT u.content AS user_content, a.content AS asst_content
               FROM chat_messages a
               JOIN chat_messages u ON u.id = a.reply_to
               WHERE a.topic = ? AND a.role = 'assistant' AND a.status = 'done'
                 AND u.content IS NOT NULL AND a.content IS NOT NULL
               ORDER BY a.id DESC LIMIT ?""",
            (topic, limit),
        ).fetchall()
    result = []
    for row in reversed(rows):
        result.append({"role": "user",      "content": row["user_content"]})
        result.append({"role": "assistant", "content": row["asst_content"]})
    return result


def get_messages(topic: Optional[str] = None, offset: int = 0, limit: int = 20) -> dict:
    """Return paginated exchange pairs (newest first) for the /history endpoint."""
    where = "WHERE a.role = 'assistant'"
    params: list = []
    if topic:
        where += " AND a.topic = ?"
        params.append(topic)

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM chat_messages a {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT a.id, a.topic, a.alias, a.backend, a.model, a.session_id,
                       u.content AS prompt, a.content AS response,
                       a.status, a.created_at AS timestamp
                FROM chat_messages a
                LEFT JOIN chat_messages u ON u.id = a.reply_to
                {where}
                ORDER BY a.id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "has_more": (offset + limit) < total,
    }


# ── session stats ─────────────────────────────────────────────────────────────

def save_stats(session_id: str, stats: dict, topic: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO session_stats
                   (session_id, topic, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, history_input_tokens,
                    cost_usd, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   topic                = COALESCE(excluded.topic, topic),
                   input_tokens         = excluded.input_tokens,
                   output_tokens        = excluded.output_tokens,
                   cache_read_tokens    = excluded.cache_read_tokens,
                   cache_write_tokens   = excluded.cache_write_tokens,
                   history_input_tokens = excluded.history_input_tokens,
                   cost_usd             = excluded.cost_usd,
                   duration_ms          = excluded.duration_ms""",
            (
                session_id, topic,
                stats.get("input_tokens", 0), stats.get("output_tokens", 0),
                stats.get("cache_read_tokens", 0), stats.get("cache_write_tokens", 0),
                stats.get("history_input_tokens", 0),
                stats.get("cost_usd"), stats.get("duration_ms"),
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
