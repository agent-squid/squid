"""
stats_db.py — Persist per-session token/cost stats that the Claude CLI
emits at stream end but never writes to its own JSONL files.

DB lives at <squid-root>/squid.db (sqlite3, stdlib only).
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent.parent / "squid.db"

_CREATE = """
CREATE TABLE IF NOT EXISTS session_stats (
    session_id              TEXT PRIMARY KEY,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    cache_read_tokens       INTEGER,
    cache_write_tokens      INTEGER,
    history_input_tokens    INTEGER DEFAULT 0,
    cost_usd                REAL,
    duration_ms             INTEGER,
    created_at              TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _backfill_created_at(conn: sqlite3.Connection) -> None:
    """Set created_at for legacy rows using the JSONL session file mtime."""
    rows = conn.execute(
        "SELECT session_id FROM session_stats WHERE created_at IS NULL"
    ).fetchall()
    if not rows:
        return
    projects_root = Path.home() / ".claude" / "projects"
    for (session_id,) in rows:
        mtime = None
        for jsonl in projects_root.rglob(f"{session_id}.jsonl"):
            mtime = jsonl.stat().st_mtime
            break
        if mtime:
            ts = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "UPDATE session_stats SET created_at = ? WHERE session_id = ?",
                (ts, session_id),
            )


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute(_CREATE)
        for migration in [
            "ALTER TABLE session_stats ADD COLUMN created_at TEXT",
            "ALTER TABLE session_stats ADD COLUMN history_input_tokens INTEGER DEFAULT 0",
            "ALTER TABLE session_stats ADD COLUMN quota_before REAL",
            "ALTER TABLE session_stats ADD COLUMN quota_after REAL",
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists
        _backfill_created_at(conn)
        conn.commit()
    finally:
        conn.close()


def save_stats(session_id: str, stats: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO session_stats
                (session_id, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens,
                 history_input_tokens, cost_usd, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                input_tokens          = excluded.input_tokens,
                output_tokens         = excluded.output_tokens,
                cache_read_tokens     = excluded.cache_read_tokens,
                cache_write_tokens    = excluded.cache_write_tokens,
                history_input_tokens  = excluded.history_input_tokens,
                cost_usd              = excluded.cost_usd,
                duration_ms           = excluded.duration_ms
            """,
            (
                session_id,
                stats.get("input_tokens", 0),
                stats.get("output_tokens", 0),
                stats.get("cache_read_tokens", 0),
                stats.get("cache_write_tokens", 0),
                stats.get("history_input_tokens", 0),
                stats.get("cost_usd"),
                stats.get("duration_ms"),
            ),
        )


def save_quota_delta(session_id: str, before: float, after: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE session_stats SET quota_before = ?, quota_after = ? WHERE session_id = ?",
            (before, after, session_id),
        )


def get_stats(session_id: str) -> Optional[dict]:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_stats WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None


def get_aggregated_stats(period: str = "daily") -> list:
    if period == "hourly":
        bucket = "strftime('%Y-%m-%d %H:00', created_at)"
        limit = 48
    else:
        bucket = "strftime('%Y-%m-%d', created_at)"
        limit = 30
    try:
        with _connect() as conn:
            rows = conn.execute(f"""
                SELECT
                    {bucket}                                                            AS period,
                    COUNT(*)                                                            AS sessions,
                    SUM(input_tokens)                                                   AS input_tokens,
                    SUM(input_tokens - COALESCE(history_input_tokens, 0))              AS new_input_tokens,
                    SUM(output_tokens)                                                  AS output_tokens,
                    SUM(cache_read_tokens)                                              AS cache_read_tokens,
                    SUM(cache_write_tokens)                                             AS cache_write_tokens,
                    SUM(cost_usd)                                                       AS cost_usd,
                    SUM(CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL
                             THEN quota_after - quota_before ELSE NULL END)             AS quota_delta
                FROM session_stats
                WHERE created_at IS NOT NULL
                GROUP BY period
                ORDER BY period DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
