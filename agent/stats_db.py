"""
stats_db.py — SQLite for stats, chat history, agents, and topics.
"""
import logging
import sqlite3
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from .config import SQUID_HOME
from .harnesses import SUPPORTED_HARNESSES, is_installed
from .resolve import agent_ref_for_storage, split_agent_ref

# Fresh-install seed models, keyed by harness — only where the harness's CLI
# default isn't the model we want new users to land on. Pi's own CLI default
# (nvidia/nemotron-3-ultra-550b-a55b) works, but deepseek-ai/deepseek-v4-pro
# is the strongest free model NVIDIA NIM currently hosts.
_SEED_DEFAULT_MODEL_BY_HARNESS: dict[str, str] = {
    "pi": "deepseek-ai/deepseek-v4-pro",
}
_SEED_DEFAULT_PROVIDER_BY_HARNESS: dict[str, str] = {
    "pi": "nvidia",
}

# Reviewer-persona agents, one per harness — cwd points at the shared
# roles/review/AGENTS.md persona (roles/review/claude/ for claudecode, since
# Claude Code reads CLAUDE.md rather than AGENTS.md).
_REVIEW_AGENT_HARNESS: dict[str, str] = {
    "clarev": "claudecode",
    "codrev": "codex",
    "operev": "opencode",
    "pirev": "pi",
    "currev": "cursor",
}

# Store database in ~/.squid/ so it persists across installs/updates.
# Override with SQUID_DB_PATH env var (e.g. for containers).
_DB_PATH = Path(os.environ.get("SQUID_DB_PATH", Path.home() / ".squid" / "squid.db"))
_SQUID_WORKTREE_PATH_RE = re.compile(r"(?:~|/[^`'\"<>\s]*)/\.squid/worktrees/[^`'\"<>\s),]+")
_CHANGED_FILES_BLOCK_RE = re.compile(r"\n*Changed files from this response:\n<changed_files>.*?</changed_files>", re.DOTALL)
_realtime_commit_listener: Optional[Callable[[int], None]] = None
try:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

_TABLES = [
    """CREATE TABLE IF NOT EXISTS session_stats (
        session_id           TEXT PRIMARY KEY,
        topic                TEXT,
        agent                TEXT,
        harness              TEXT,
        provider             TEXT,
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
        adhoc                INTEGER DEFAULT 0,
        lookback             INTEGER DEFAULT 0,
        created_at           TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS agents (
        name       TEXT PRIMARY KEY,
        harness    TEXT,
        provider   TEXT,
        model      TEXT,
        cwd        TEXT,
        timeout    INTEGER,
        home_mode  TEXT NOT NULL DEFAULT 'user_home',
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
        last_session_at    TEXT,
        last_adhoc_at      TEXT,
        last_model         TEXT,
        last_harness       TEXT,
        last_provider      TEXT,
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
        source      TEXT NOT NULL DEFAULT 'human',
        content     TEXT,
        reply_to    INTEGER REFERENCES chat_messages(id),
        status      TEXT NOT NULL DEFAULT 'pending',
        adhoc       INTEGER DEFAULT 0,
        context     TEXT,
        status_raw  TEXT,
        flow_run_id TEXT,
        flow_route  TEXT,
        session_turn_index INTEGER,
        lookback    INTEGER DEFAULT 0,
        quota_delta  REAL,
        quota_before REAL,
        quota_after  REAL,
        completed_at TEXT,
        created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS id_counters (
        namespace TEXT PRIMARY KEY,
        next_id   INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS topic_sessions (
        topic       TEXT NOT NULL,
        agent       TEXT NOT NULL,
        session_id  TEXT NOT NULL,
        cwd         TEXT NOT NULL,
        runtime_fingerprint TEXT,
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
    """CREATE TABLE IF NOT EXISTS realtime_events (
        event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        topic      TEXT,
        agent      TEXT,
        msg_id     INTEGER,
        run_seq    INTEGER,
        payload    TEXT NOT NULL DEFAULT '{}',
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )""",
    """CREATE INDEX IF NOT EXISTS idx_realtime_events_scope
       ON realtime_events(topic, agent, event_id)""",
    """CREATE TABLE IF NOT EXISTS realtime_requests (
        principal   TEXT NOT NULL,
        request_id  TEXT NOT NULL,
        request_type TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        result      TEXT NOT NULL,
        created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (principal, request_id)
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
    """CREATE TABLE IF NOT EXISTS message_annotations (
        msg_id     INTEGER NOT NULL,
        kind       TEXT NOT NULL,
        payload    TEXT,
        created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (msg_id, kind)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_message_annotations_kind ON message_annotations (kind, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS worktrees (
        topic           TEXT NOT NULL,
        agent           TEXT NOT NULL,
        repo_root       TEXT NOT NULL,
        worktree_path   TEXT NOT NULL,
        branch_name     TEXT NOT NULL,
        base_commit     TEXT,
        integration_worktree_path TEXT,
        status          TEXT NOT NULL DEFAULT 'pending',
        created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        last_used_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (topic, agent, repo_root)
    )""",
    """CREATE TABLE IF NOT EXISTS stats_filter_presets (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
        state_json    TEXT NOT NULL,
        is_default    INTEGER NOT NULL DEFAULT 0,
        display_order INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_stats_filter_presets_one_default
       ON stats_filter_presets(is_default)
       WHERE is_default = 1""",
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

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_usage_stats() -> dict:
    """Return total prompts and first-seen date from chat history."""
    try:
        db = _connect()
        total = db.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE role = 'user'"
        ).fetchone()[0]
        first = db.execute(
            "SELECT MIN(created_at) FROM chat_messages"
        ).fetchone()[0]
        db.close()
        return {"total_prompts": total, "first_seen": first}
    except Exception:
        return {"total_prompts": 0, "first_seen": None}


def _runtime_ref_expr(harness_col: str = "harness", provider_col: str = "provider") -> str:
    return (
        f"CASE WHEN {harness_col} IS NULL THEN NULL "
        f"WHEN {provider_col} IS NULL OR {provider_col} = '' THEN {harness_col} "
        f"ELSE {harness_col} || ':' || {provider_col} END"
    )


def _stats_agent_expr(prefix: str = "") -> str:
    return f"COALESCE({prefix}agent, {_runtime_ref_expr(prefix + 'harness', prefix + 'provider')}, 'unknown')"


def _with_backend(item: dict, *, harness_key: str = "harness", provider_key: str = "provider", output_key: str = "backend") -> dict:
    item[output_key] = agent_ref_for_storage(item[harness_key], item.get(provider_key)) if item.get(harness_key) else None
    return item


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _migrate_message_annotations_schema(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "message_annotations")
    target = {"msg_id", "kind", "payload", "created_at", "updated_at"}
    if columns <= target:
        return
    conn.execute(
        """CREATE TABLE message_annotations_new (
            msg_id     INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            payload    TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (msg_id, kind)
        )"""
    )
    conn.execute(
        """INSERT OR REPLACE INTO message_annotations_new
           (msg_id, kind, payload, created_at, updated_at)
           SELECT msg_id, kind, COALESCE(payload, '{}'), created_at, updated_at
           FROM message_annotations"""
    )
    conn.execute("DROP TABLE message_annotations")
    conn.execute("ALTER TABLE message_annotations_new RENAME TO message_annotations")


def _migrate_bookmarks_to_annotations(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "bookmarks"):
        return
    conn.execute(
        """INSERT OR IGNORE INTO message_annotations
           (msg_id, kind, payload, created_at, updated_at)
           SELECT msg_id, 'bookmark', '{}', saved_at, saved_at
           FROM bookmarks"""
    )
    conn.execute("DROP TABLE bookmarks")


def allocate_id(namespace: str) -> str:
    namespace = (namespace or "").strip()
    if not namespace:
        raise ValueError("namespace is required")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO id_counters (namespace, next_id) VALUES (?, 1)",
            (namespace,),
        )
        row = conn.execute(
            "SELECT next_id FROM id_counters WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        next_id = int(row["next_id"])
        conn.execute(
            "UPDATE id_counters SET next_id = ? WHERE namespace = ?",
            (next_id + 1, namespace),
        )
        conn.commit()
        return str(next_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = _connect()
    try:
        for ddl in _TABLES:
            conn.execute(ddl)
        _migrate_message_annotations_schema(conn)
        _migrate_bookmarks_to_annotations(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_message_annotations_kind ON message_annotations (kind, created_at DESC)")
        topic_columns = _table_columns(conn, "topics")
        if "last_session_at" not in topic_columns:
            conn.execute("ALTER TABLE topics ADD COLUMN last_session_at TEXT")
        if "last_adhoc_at" not in topic_columns:
            conn.execute("ALTER TABLE topics ADD COLUMN last_adhoc_at TEXT")
        message_columns = _table_columns(conn, "chat_messages")
        if "source" not in message_columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN source TEXT NOT NULL DEFAULT 'human'")
            conn.execute("UPDATE chat_messages SET source = 'human' WHERE source IS NULL OR source = ''")
        # 'system' was renamed to 'workflow' (flow-route handoffs were its only
        # use); backfill existing rows written under the old name.
        conn.execute("UPDATE chat_messages SET source = 'workflow' WHERE source = 'system'")
        if "completed_at" not in message_columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN completed_at TEXT")
        if "flow_run_id" not in message_columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN flow_run_id TEXT")
        if "flow_route" not in message_columns:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN flow_route TEXT")
        worktree_columns = _table_columns(conn, "worktrees")
        if "base_commit" not in worktree_columns:
            conn.execute("ALTER TABLE worktrees ADD COLUMN base_commit TEXT")
        if "integration_worktree_path" not in worktree_columns:
            conn.execute("ALTER TABLE worktrees ADD COLUMN integration_worktree_path TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_flow_route ON chat_messages(flow_route, completed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_flow_run ON chat_messages(flow_run_id, id)")
        agent_columns = _table_columns(conn, "agents")
        if "home_mode" not in agent_columns:
            conn.execute("ALTER TABLE agents ADD COLUMN home_mode TEXT NOT NULL DEFAULT 'user_home'")
        has_legacy_backend = "backend" in agent_columns
        # Seed one default agent per installed harness (INSERT OR IGNORE — never overwrites user edits)
        for harness in sorted(SUPPORTED_HARNESSES):
            if harness != "claudecode" and is_installed(harness):
                provider = _SEED_DEFAULT_PROVIDER_BY_HARNESS.get(harness)
                model = _SEED_DEFAULT_MODEL_BY_HARNESS.get(harness)
                if has_legacy_backend:
                    conn.execute(
                        """INSERT OR IGNORE INTO agents
                           (name, backend, harness, provider, model) VALUES (?, ?, ?, ?, ?)""",
                        (harness, agent_ref_for_storage(harness, provider), harness, provider, model),
                    )
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO agents (name, harness, provider, model) VALUES (?, ?, ?, ?)",
                        (harness, harness, provider, model),
                    )
        if is_installed("claudecode"):
            if has_legacy_backend:
                conn.execute(
                    """INSERT OR IGNORE INTO agents
                       (name, backend, harness, provider, model) VALUES (?, ?, ?, ?, ?)""",
                    ("claude", "claudecode:anthropic", "claudecode", "anthropic", None),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO agents (name, harness, provider, model) VALUES (?, ?, ?, ?)",
                    ("claude", "claudecode", "anthropic", None),
                )
        # Seed one reviewer-persona agent per installed harness (same
        # INSERT OR IGNORE contract as the default agents above).
        for agent_name, harness in sorted(_REVIEW_AGENT_HARNESS.items()):
            if not is_installed(harness):
                continue
            provider = "anthropic" if harness == "claudecode" else _SEED_DEFAULT_PROVIDER_BY_HARNESS.get(harness)
            model = None if harness == "claudecode" else _SEED_DEFAULT_MODEL_BY_HARNESS.get(harness)
            cwd = f"{SQUID_HOME}/roles/review/claude" if harness == "claudecode" else f"{SQUID_HOME}/roles/review"
            if has_legacy_backend:
                conn.execute(
                    """INSERT OR IGNORE INTO agents
                       (name, backend, harness, provider, model, cwd) VALUES (?, ?, ?, ?, ?, ?)""",
                    (agent_name, agent_ref_for_storage(harness, provider), harness, provider, model, cwd),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO agents (name, harness, provider, model, cwd) VALUES (?, ?, ?, ?, ?)",
                    (agent_name, harness, provider, model, cwd),
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
        rows = [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name").fetchall()]
    for row in rows:
        if not row.get("harness"):
            row["harness"], row["provider"] = split_agent_ref(row.get("backend"), row.get("provider"))
        _with_backend(row)
    return rows


def get_default_agent() -> Optional[dict]:
    """Return the first available agent in fallback order: claude → codex → cursor."""
    with _connect() as conn:
        rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM agents").fetchall()}
    for name in ("claude", "codex", "cursor", "opencode", "pi", "claudecode"):
        if name in rows:
            row = rows[name]
            if not row.get("harness"):
                row["harness"], row["provider"] = split_agent_ref(row.get("backend"), row.get("provider"))
            _with_backend(row)
            return row
    # Any agent at all
    row = next(iter(rows.values()), None) if rows else None
    if row and not row.get("harness"):
        row["harness"], row["provider"] = split_agent_ref(row.get("backend"), row.get("provider"))
    if row:
        _with_backend(row)
    return row


def get_agent(name: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    if not row:
        return None
    item = dict(row)
    if not item.get("harness"):
        item["harness"], item["provider"] = split_agent_ref(item.get("backend"), item.get("provider"))
    _with_backend(item)
    return item


def upsert_agent(name: str, harness: str, provider: Optional[str], model: Optional[str],
                 cwd: Optional[str] = None) -> bool:
    """Upsert agent config. Returns True if key attributes changed."""
    with _connect() as conn:
        existing = conn.execute("SELECT harness, provider, model, cwd FROM agents WHERE name = ?", (name,)).fetchone()
        key_changed = existing and (
            existing["harness"] != harness or
            existing["provider"] != provider or
            existing["model"] != model or
            existing["cwd"] != cwd
        )
        if "backend" in _table_columns(conn, "agents"):
            cur = conn.execute(
                """INSERT INTO agents (name, backend, harness, provider, model, cwd)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     backend  = excluded.backend,
                     harness  = excluded.harness,
                     provider = excluded.provider,
                     model    = excluded.model,
                     cwd      = excluded.cwd""",
                (name, agent_ref_for_storage(harness, provider), harness, provider, model, cwd),
            )
        else:
            cur = conn.execute(
                """INSERT INTO agents (name, harness, provider, model, cwd) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     harness = excluded.harness,
                     provider = excluded.provider,
                     model   = excluded.model,
                     cwd     = excluded.cwd""",
                (name, harness, provider, model, cwd),
            )
    return bool(key_changed)


def get_agent_home_mode(name: str) -> str:
    """'user_home' (default, full inheritance) or 'blank_home' -- see ADR-0036."""
    with _connect() as conn:
        row = conn.execute("SELECT home_mode FROM agents WHERE name = ?", (name,)).fetchone()
    return row["home_mode"] if row and row["home_mode"] else "user_home"


def set_agent_home_mode(name: str, home_mode: str) -> None:
    if home_mode not in ("user_home", "blank_home"):
        raise ValueError(f"invalid home_mode: {home_mode!r}")
    with _connect() as conn:
        conn.execute("UPDATE agents SET home_mode = ? WHERE name = ?", (home_mode, name))


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
    last_backend_expr = _runtime_ref_expr("last_harness", "last_provider")
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT topic AS name, sticky_agent AS agent, sticky_adhoc,
                      last_model, last_harness, last_provider,
                      {last_backend_expr} AS last_backend, last_prompt, last_at
               FROM topics
               WHERE agent = '' AND hidden = 0
               ORDER BY last_at DESC NULLS LAST"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_topics_management_summary(include_hidden: bool = True) -> list[dict]:
    where_hidden = "" if include_hidden else "AND hidden = 0"
    last_backend_expr = _runtime_ref_expr("last_harness", "last_provider")
    with _connect() as conn:
        topic_rows = conn.execute(
            f"""SELECT topic AS name, sticky_agent AS agent, sticky_adhoc,
                       last_model, last_harness, last_provider,
                       {last_backend_expr} AS last_backend, last_prompt, last_at, hidden
                FROM topics
                WHERE agent = '' {where_hidden}
                ORDER BY last_at DESC NULLS LAST, topic ASC"""
        ).fetchall()
        agent_rows = conn.execute(
            f"""SELECT topic, agent, last_prompt, last_adhoc_prompt, last_at,
                      COALESCE(last_session_at, CASE WHEN last_prompt IS NOT NULL THEN last_at END) AS last_session_at,
                      COALESCE(last_adhoc_at, CASE WHEN last_adhoc_prompt IS NOT NULL THEN last_at END) AS last_adhoc_at,
                      last_model, last_harness, last_provider,
                      {last_backend_expr} AS last_backend
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
        session_time_rows = conn.execute(
            """SELECT topic, agent, MAX(created_at) AS last_session_at
               FROM chat_messages
               WHERE role = 'assistant' AND (adhoc = 0 OR adhoc IS NULL)
               GROUP BY topic, agent"""
        ).fetchall()
        adhoc_time_rows = conn.execute(
            """SELECT topic, agent, MAX(created_at) AS last_adhoc_at
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
    session_at_by_key: dict[tuple, str] = {
        (r["topic"], r["agent"]): r["last_session_at"] for r in session_time_rows
    }
    adhoc_at_by_key: dict[tuple, str] = {
        (r["topic"], r["agent"]): r["last_adhoc_at"] for r in adhoc_time_rows
    }
    live_turns_by_key: dict[tuple, int] = {
        (r["topic"], r["agent"]): r["live_turns"] for r in live_turn_rows
    }

    agents_by_topic: dict[str, list[dict]] = {}
    for row in agent_rows:
        item = dict(row)
        topic = item.pop("topic")
        key = (topic, item["agent"])
        item["last_session_at"] = session_at_by_key.get(key) or item.get("last_session_at")
        item["last_adhoc_at"] = adhoc_at_by_key.get(key) or item.get("last_adhoc_at")
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
    last_harness: Optional[str] = None,
    last_provider: Optional[str] = None,
    adhoc: bool = False,
) -> None:
    now = __import__('time').strftime("%Y-%m-%dT%H:%M:%SZ", __import__('time').gmtime())
    at = now if last_prompt else None
    with _connect() as conn:
        # Topic-level row
        conn.execute(
            """INSERT INTO topics (topic, agent, sticky_agent, sticky_adhoc, last_prompt, last_at, last_model, last_harness, last_provider)
               VALUES (?, '', ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(topic, agent) DO UPDATE SET
                 hidden       = 0,
                 sticky_agent = CASE WHEN excluded.sticky_agent IS NOT NULL THEN excluded.sticky_agent ELSE sticky_agent END,
                 sticky_adhoc = CASE WHEN excluded.sticky_agent IS NOT NULL THEN excluded.sticky_adhoc ELSE sticky_adhoc END,
                 last_prompt  = COALESCE(excluded.last_prompt, last_prompt),
                 last_at      = COALESCE(excluded.last_at, last_at),
                 last_model   = COALESCE(excluded.last_model, last_model),
                 last_harness = COALESCE(excluded.last_harness, last_harness),
                 last_provider = COALESCE(excluded.last_provider, last_provider)""",
            (name, agent, 1 if adhoc else 0, last_prompt, at, last_model, last_harness, last_provider),
        )
        # Agent-level row
        if agent:
            session_prompt = None if adhoc else last_prompt
            adhoc_prompt   = last_prompt if adhoc else None
            session_at = at if session_prompt else None
            adhoc_at = at if adhoc_prompt else None
            conn.execute(
                """INSERT INTO topics (topic, agent, last_prompt, last_adhoc_prompt, last_at, last_session_at, last_adhoc_at, last_model, last_harness, last_provider)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(topic, agent) DO UPDATE SET
                     last_prompt        = COALESCE(excluded.last_prompt, last_prompt),
                     last_adhoc_prompt  = COALESCE(excluded.last_adhoc_prompt, last_adhoc_prompt),
                     last_at            = COALESCE(excluded.last_at, last_at),
                     last_session_at    = COALESCE(excluded.last_session_at, last_session_at),
                     last_adhoc_at      = COALESCE(excluded.last_adhoc_at, last_adhoc_at),
                     last_model         = COALESCE(excluded.last_model, last_model),
                     last_harness       = COALESCE(excluded.last_harness, last_harness),
                     last_provider      = COALESCE(excluded.last_provider, last_provider)""",
                (name, agent, session_prompt, adhoc_prompt, at, session_at, adhoc_at, last_model, last_harness, last_provider),
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
        conn.execute(
            "DELETE FROM message_annotations WHERE msg_id IN (SELECT id FROM chat_messages WHERE topic=?)",
            (name,),
        )
        conn.execute("DELETE FROM topic_sessions WHERE topic = ?", (name,))
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
            conn.execute(
                "DELETE FROM message_annotations WHERE msg_id IN (SELECT id FROM chat_messages WHERE topic=? AND agent=?)",
                (topic, agent),
            )
            conn.execute("DELETE FROM chat_messages WHERE topic=? AND agent=?", (topic, agent))
            conn.execute("DELETE FROM topic_sessions WHERE topic=? AND agent=?", (topic, agent))
            conn.execute("DELETE FROM topics WHERE topic=? AND agent=?", (topic, agent))
        elif adhoc:
            conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM chat_messages WHERE topic=? AND agent=? AND adhoc=1 AND role='assistant')",
                (topic, agent),
            )
            conn.execute(
                "DELETE FROM message_annotations WHERE msg_id IN (SELECT id FROM chat_messages WHERE topic=? AND agent=? AND adhoc=1)",
                (topic, agent),
            )
            conn.execute(
                "DELETE FROM chat_messages WHERE topic=? AND agent=? AND adhoc=1",
                (topic, agent),
            )
            conn.execute(
                "UPDATE topics SET last_adhoc_prompt=NULL, last_adhoc_at=NULL WHERE topic=? AND agent=?",
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
                "DELETE FROM message_annotations WHERE msg_id IN (SELECT id FROM chat_messages WHERE topic=? AND agent=? AND (adhoc=0 OR adhoc IS NULL))",
                (topic, agent),
            )
            conn.execute(
                "DELETE FROM chat_messages WHERE topic=? AND agent=? AND (adhoc=0 OR adhoc IS NULL)",
                (topic, agent),
            )
            conn.execute("DELETE FROM topic_sessions WHERE topic=? AND agent=?", (topic, agent))
            conn.execute(
                "UPDATE topics SET last_prompt=NULL, last_session_at=NULL WHERE topic=? AND agent=?",
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

def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _flow_route_variants(flow_route: str) -> list[str]:
    route = (flow_route or "").strip()
    match = re.fullmatch(r"#(\w+)@(\w+)(!)?>@(\w+)(!)?", route)
    if not match:
        return [route] if route else []
    if match.group(3) or match.group(5):
        return [route]
    topic, origin, target = match.group(1), match.group(2), match.group(4)
    return [
        f"#{topic}@{origin}>@{target}",
        f"#{topic}@{origin}!>@{target}",
        f"#{topic}@{origin}>@{target}!",
        f"#{topic}@{origin}!>@{target}!",
    ]


def _flow_route_filter_clause(flow_route: str, msg_alias: str = "m") -> tuple[str, list]:
    routes = _flow_route_variants(flow_route)
    if not routes:
        return "0", []

    params: list = []
    placeholders = ",".join("?" for _ in routes)
    clauses = [f"{msg_alias}.flow_route IN ({placeholders})"]
    params.extend(routes)

    target_clauses = []
    target_params: list = []
    origin_clauses = []
    origin_params: list = []
    for route in routes:
        route_like = f"%Route: {_like_escape(route)}%"
        target_clauses.append(
            f"""EXISTS (
                SELECT 1 FROM chat_messages pu
                WHERE pu.id = {msg_alias}.reply_to
                  AND pu.source = 'workflow'
                  AND pu.content LIKE ? ESCAPE '\\'
            )"""
        )
        target_params.append(route_like)

        match = re.fullmatch(r"#(\w+)@(\w+)(!)?>@(\w+)(!)?", route)
        if match:
            origin_clauses.append(
                f"""({msg_alias}.topic = ? AND {msg_alias}.agent = ?
                    AND COALESCE({msg_alias}.adhoc, 0) = ?
                    AND EXISTS (
                        SELECT 1 FROM chat_messages su
                        WHERE su.role = 'user'
                          AND su.source = 'workflow'
                          AND su.topic = {msg_alias}.topic
                          AND su.content LIKE ? ESCAPE '\\'
                          AND su.id > {msg_alias}.id
                    ))"""
            )
            origin_params.extend([match.group(1).lower(), match.group(2), 1 if match.group(3) else 0, route_like])

    clauses.extend(target_clauses)
    clauses.extend(origin_clauses)
    params.extend(target_params)
    params.extend(origin_params)
    return "(" + " OR ".join(clauses) + ")", params


def _route_from_handoff_prompt(prompt: Optional[str]) -> Optional[str]:
    match = re.search(r"(?:^|\n)Route:\s*(#\w+@\w+!?>@\w+!?)(?:\s|$)", prompt or "")
    return match.group(1) if match else None


def _infer_origin_flow_route(conn: sqlite3.Connection, row: dict) -> Optional[str]:
    if row.get("flow_route") or row.get("prompt_source") == "workflow":
        return row.get("flow_route")
    if row.get("id") is None or not row.get("topic") or not row.get("agent"):
        return row.get("flow_route")
    route_rows = conn.execute(
        """SELECT content
           FROM chat_messages
           WHERE role = 'user'
             AND source = 'workflow'
             AND topic = ?
             AND id > ?
             AND content LIKE '%Route: %'
           ORDER BY id ASC
           LIMIT 20""",
        (row["topic"], row["id"]),
    ).fetchall()
    for route_row in route_rows:
        route = _route_from_handoff_prompt(route_row["content"])
        parts = re.fullmatch(r"#(\w+)@(\w+)(!)?>@(\w+)(!)?", route or "")
        if (
            parts
            and parts.group(1).lower() == row["topic"]
            and parts.group(2) == row["agent"]
            and bool(parts.group(3)) == bool(row.get("adhoc"))
        ):
            return route
    return row.get("flow_route")

def insert_user_message(
    topic: str, agent: Optional[str],
    content: str, context_ids: Optional[list[int]] = None,
    mem: bool = False,
    mem_revision: Optional[str] = None,
    lookback: int = 0,
    source: str = "human",
    flow_run_id: Optional[str] = None,
    flow_route: Optional[str] = None,
) -> int:
    if source not in {"human", "workflow", "diff_viewer"}:
        source = "human"
    if context_ids or mem or mem_revision:
        context = {"pins": context_ids or [], "mem": mem}
        if mem_revision:
            context["mem_revision"] = mem_revision
        context_json = json.dumps(context)
    else:
        context_json = None
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (topic, agent, role, source, content, status, context, lookback, flow_run_id, flow_route)
               VALUES (?, ?, 'user', ?, ?, 'done', ?, ?, ?, ?)""",
            (topic, agent, source, content, context_json, lookback, flow_run_id, flow_route),
        )
        _insert_realtime_event(conn, "message.changed", topic, agent, cur.lastrowid, None, {
            "id": cur.lastrowid, "role": "user", "status": "done", "content": content,
        })
        return cur.lastrowid


def insert_assistant_message(
    topic: str, agent: Optional[str],
    reply_to: int, adhoc: bool = False,
    flow_run_id: Optional[str] = None,
    flow_route: Optional[str] = None,
    source: str = "human",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (topic, agent, role, source, reply_to, status, adhoc, flow_run_id, flow_route)
               VALUES (?, ?, 'assistant', ?, ?, 'pending', ?, ?, ?)""",
            (topic, agent, source, reply_to, 1 if adhoc else 0, flow_run_id, flow_route),
        )
        _insert_realtime_event(conn, "message.changed", topic, agent, cur.lastrowid, None, {
            "id": cur.lastrowid, "role": "assistant", "status": "pending", "reply_to": reply_to,
        })
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


def get_session_turn_boundaries(session_id: str) -> list[dict]:
    """Ordered (msg_id, turn_index, time_ms) for every completed turn squid
    recorded against this session_id -- squid's own turn grouping, which a
    raw session log (jsonl file or opencode's SQLite rows) has no notion of
    on its own. `created_at` is when the turn's prompt was sent, i.e. the
    start of everything that turn's log entries belong to."""
    from datetime import datetime, timezone
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id AS msg_id, session_turn_index, created_at
               FROM chat_messages
               WHERE session_id = ? AND role = 'assistant' AND session_turn_index IS NOT NULL
               ORDER BY session_turn_index""",
            (session_id,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        dt = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out.append({
            "msg_id": d["msg_id"],
            "turn_index": d["session_turn_index"],
            "time_ms": int(dt.timestamp() * 1000),
        })
    return out


def attach_assistant_session(msg_id: int, session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE chat_messages
               SET session_id = COALESCE(session_id, ?)
               WHERE id = ? AND role = 'assistant'""",
            (session_id, msg_id),
        )
        return cur.rowcount > 0


def rebind_pending_assistant_session(msg_id: int, session_id: Optional[str]) -> bool:
    """Replace enqueue-time session metadata before a queued turn starts."""
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE chat_messages SET session_id = ?
               WHERE id = ? AND role = 'assistant' AND status = 'pending'
                 AND session_turn_index IS NULL""",
            (session_id, msg_id),
        )
        return cur.rowcount > 0


def update_assistant_message(
    msg_id: int, content: str, session_id: Optional[str], status: str = "done",
    context: Optional[str] = None,
    status_raw: Optional[str] = None,
    only_if_pending: bool = False,
) -> None:
    if status == "done":
        status_raw = _sanitize_status_raw(content, status_raw)
    terminal = status in {"done", "error", "cancelled"}
    with _connect() as conn:
        if only_if_pending:
            cur = conn.execute(
                "UPDATE chat_messages SET content=?, session_id=?, status=?, context=?, status_raw=?,"
                " completed_at=CASE WHEN ? THEN COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) ELSE completed_at END"
                " WHERE id=? AND status='pending'",
                (content, session_id, status, context, status_raw, 1 if terminal else 0, msg_id),
            )
            if cur.rowcount:
                _ensure_session_turn_index(conn, msg_id, session_id)
        else:
            cur = conn.execute(
                "UPDATE chat_messages SET content=?, session_id=?, status=?, context=?, status_raw=?,"
                " completed_at=CASE WHEN ? THEN COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) ELSE completed_at END"
                " WHERE id=?",
                (content, session_id, status, context, status_raw, 1 if terminal else 0, msg_id),
            )
            if status == "done":
                _ensure_session_turn_index(conn, msg_id, session_id)
        row = conn.execute("SELECT topic, agent FROM chat_messages WHERE id=?", (msg_id,)).fetchone()
        if cur.rowcount and row:
            _insert_realtime_event(conn, "message.changed", row["topic"], row["agent"], msg_id, None, {
                "id": msg_id, "role": "assistant", "status": status, "content": content,
                "session_id": session_id,
            })


def _run_event_cancel_snapshot(conn: sqlite3.Connection, msg_id: int) -> dict:
    return _run_event_snapshot(conn, msg_id)


def _run_event_snapshot(conn: sqlite3.Connection, msg_id: int) -> dict:
    rows = conn.execute(
        "SELECT event_type, payload FROM run_events WHERE msg_id=? ORDER BY seq",
        (msg_id,),
    ).fetchall()
    text = "".join(row["payload"] or "" for row in rows if row["event_type"] == "text")
    status_raw = "".join(row["payload"] or "" for row in rows if row["event_type"] == "status")
    tools = []
    session_id = None
    for row in rows:
        payload = row["payload"]
        if row["event_type"] == "tool" and payload:
            try:
                tools.append(json.loads(payload))
            except Exception:
                tools.append(payload)
        elif row["event_type"] == "stats" and payload and not session_id:
            try:
                stats = json.loads(payload)
            except Exception:
                stats = {}
            session_id = stats.get("session_id") or None
    return {
        "text": text or None,
        "status_raw": _sanitize_status_raw(text, status_raw) or status_raw or None,
        "context": json.dumps(tools) if tools else None,
        "session_id": session_id,
    }


def get_run_event_snapshot(msg_id: int) -> dict:
    with _connect() as conn:
        return _run_event_snapshot(conn, msg_id)


def mark_assistant_cancelled(msg_id: int, reason: str = "Cancelled") -> bool:
    with _connect() as conn:
        snapshot = _run_event_cancel_snapshot(conn, msg_id)
        cur = conn.execute(
            """UPDATE chat_messages
               SET content = COALESCE(NULLIF(content, ''), ?, ?),
                   session_id = COALESCE(session_id, ?),
                   context = COALESCE(context, ?),
                   status = 'cancelled',
                   status_raw = COALESCE(NULLIF(status_raw, ''), ?, ?),
                   completed_at = COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
               WHERE id = ? AND role = 'assistant' AND status = 'pending'""",
            (
                snapshot["text"], reason,
                snapshot["session_id"],
                snapshot["context"],
                snapshot["status_raw"], reason,
                msg_id,
            ),
        )
        if cur.rowcount:
            row = conn.execute("SELECT topic, agent, content FROM chat_messages WHERE id=?", (msg_id,)).fetchone()
            _insert_realtime_event(conn, "message.changed", row["topic"], row["agent"], msg_id, None, {
                "id": msg_id, "role": "assistant", "status": "cancelled", "content": row["content"] or "",
            })
        return cur.rowcount > 0


def _sanitize_status_raw(content: Optional[str], status_raw: Optional[str]) -> Optional[str]:
    if not content or not status_raw:
        return status_raw
    final_text = content.strip()
    trace_text = status_raw.strip()
    if not final_text or not trace_text:
        return status_raw
    if trace_text == final_text:
        return None
    stripped = False
    while trace_text.endswith(final_text):
        trace_text = trace_text[: -len(final_text)].rstrip()
        stripped = True
        if not trace_text:
            return None
    if stripped:
        return trace_text or None
    return status_raw


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
            "SELECT session_id, cwd, runtime_fingerprint FROM topic_sessions WHERE topic=? AND agent=?",
            (topic, agent),
        ).fetchone()
    return dict(row) if row else None


def get_session_turn_count(session_id: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            """SELECT COALESCE(MAX(session_turn_index), 0) AS count
               FROM chat_messages
               WHERE session_id=? AND role='assistant' AND adhoc=0""",
            (session_id,),
        ).fetchone()
    return int(row["count"] or 0) if row else 0


_invalidated_session_ids: set[str] = set()


def set_topic_session(topic: str, agent: str, session_id: str, cwd: Optional[str],
                      runtime_fingerprint: Optional[str] = None) -> None:
    if session_id in _invalidated_session_ids:
        _invalidated_session_ids.discard(session_id)
        return
    with _connect() as conn:
        conn.execute(
            """INSERT INTO topic_sessions (topic, agent, session_id, cwd, runtime_fingerprint) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(topic, agent) DO UPDATE SET session_id=excluded.session_id,
                 cwd=excluded.cwd,
                 runtime_fingerprint=excluded.runtime_fingerprint""",
            (topic, agent, session_id, cwd, runtime_fingerprint),
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


def _gitdiff_context_summary(context: Optional[str]) -> str:
    if not context:
        return ""
    try:
        tools = json.loads(context)
    except Exception:
        return ""
    if not isinstance(tools, list):
        return ""

    blocks = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("name") != "GitDiff":
            continue
        files = [f for f in tool.get("files", []) if isinstance(f, dict) and f.get("path")]
        if not files:
            continue

        repo = tool.get("repo") or tool.get("source") or tool.get("cwd")
        file_count = tool.get("file_count") or len(files)
        additions = tool.get("additions")
        deletions = tool.get("deletions")
        total = f"{file_count} file{'s' if file_count != 1 else ''}"
        if isinstance(additions, int) and isinstance(deletions, int):
            total = f"{total}, +{additions} -{deletions}"

        lines = ["Changed files from this response:", "<changed_files>"]
        if repo:
            lines.append(f"Repo: {repo}")
        lines.append(f"Summary: {total}")
        stat = (tool.get("stat") or "").strip()
        if stat:
            lines.extend(["Stat:", stat])
        lines.append("Files:")
        for f in files:
            status = f.get("status") or "?"
            path = f.get("path")
            old_path = f.get("old_path")
            if old_path and old_path != path:
                lines.append(f"- {status} {old_path} -> {path}")
            else:
                lines.append(f"- {status} {path}")
        lines.append("</changed_files>")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def _sanitize_pinned_assistant_content(content: str) -> str:
    content = _CHANGED_FILES_BLOCK_RE.sub("", content).rstrip()
    return _SQUID_WORKTREE_PATH_RE.sub("[temporary Squid worktree]", content)


def get_messages_by_ids(ids: list[int]) -> list[dict]:
    """Fetch specific assistant messages by ID as context history pairs.
    Returns [user, asst, ...] dicts in ascending ID order. Only done rows with content."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT a.id, a.topic, a.agent, u.content AS user_content, a.content AS asst_content, a.context
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
        asst_content = _sanitize_pinned_assistant_content(row["asst_content"])
        gitdiff_summary = _gitdiff_context_summary(row["context"])
        if gitdiff_summary:
            asst_content = f"{asst_content.rstrip()}\n\n{gitdiff_summary}"
        result.extend([
            {"role": "user",      "content": row["user_content"], "topic": row["topic"], "agent": row["agent"]},
            {"role": "assistant", "content": asst_content,        "topic": row["topic"], "agent": row["agent"]},
        ])
    return result


def get_message_previews(ids: list[int], max_chars: int = 120) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id,
                       substr(
                         COALESCE(NULLIF(trim(m.content), ''), NULLIF(trim(u.content), ''), '(empty)'),
                         1,
                         ?
                       ) AS preview
                FROM chat_messages m
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                WHERE m.id IN ({placeholders})
                ORDER BY m.id ASC""",
            [max_chars, *ids],
        ).fetchall()
    return [{"id": row["id"], "preview": row["preview"] or "(empty)"} for row in rows]


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


def get_flow_run_messages(flow_run_id: str) -> list[dict]:
    """All chat_messages rows for one Squid Flow run, ascending by id — the
    full step sequence used to derive what (if anything) runs next."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, role, topic, agent, source, reply_to, adhoc, content, status, context, flow_route, created_at
               FROM chat_messages
               WHERE flow_run_id = ?
               ORDER BY id""",
            (flow_run_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_flow_run_id_for_message(msg_id: int) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT flow_run_id FROM chat_messages WHERE id = ?",
            (msg_id,),
        ).fetchone()
        return row["flow_run_id"] if row else None


def get_flow_run_ids_with_row_counts(counts: tuple[int, ...]) -> list[str]:
    """Distinct flow_run_ids whose total message count matches one of `counts` —
    used to cheaply narrow a boot-time sweep to runs that look mid-chain rather
    than complete."""
    placeholders = ",".join("?" * len(counts))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT flow_run_id FROM chat_messages
                WHERE flow_run_id IS NOT NULL
                GROUP BY flow_run_id
                HAVING COUNT(*) IN ({placeholders})""",
            counts,
        ).fetchall()
        return [row["flow_run_id"] for row in rows]


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
                    """UPDATE chat_messages
                       SET content=?, status='done', status_raw=COALESCE(status_raw, ?),
                           completed_at=COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                       WHERE id=?""",
                    (final_text, status_raw, row["id"]),
                )
                _ensure_session_turn_index(conn, row["id"], row["session_id"])
            else:
                conn.execute(
                    """UPDATE chat_messages
                       SET content='', status='error',
                           completed_at=COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                       WHERE id=?""",
                    (row["id"],),
                )
            count += 1
        return count


def get_message(msg_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                      m.content, m.status, m.source, m.adhoc, m.session_id,
                      m.flow_run_id, m.flow_route,
                      m.context, m.status_raw, m.created_at AS timestamp,
                      {_turn_end_expr("m")} AS completed_at, m.reply_to,
                      m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                      u.content AS prompt, u.context AS prompt_context, u.source AS prompt_source,
                      m.session_turn_index AS session_turn_count,
                      re.payload AS stats_payload,
                      {_marked_bad_expr("m")} AS marked_bad
               FROM chat_messages m
               LEFT JOIN chat_messages u ON m.reply_to = u.id
               {_latest_stats_event_join(msg_alias="m", outer=True)}
               WHERE m.id=?""",
            (msg_id,)
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    _attach_turn_stats(result)
    return result


def get_messages_flat(topic: Optional[str] = None, agent: Optional[str] = None,
                      adhoc: Optional[bool] = None, offset: int = 0, limit: int = 20,
                      flow_route: Optional[str] = None, bookmarked: bool = False,
                      marked_bad: bool = False) -> dict:
    where = "WHERE m.role = 'assistant'"
    params: list = []
    bookmark_join = (
        "JOIN message_annotations ma_bookmark ON ma_bookmark.msg_id = m.id AND ma_bookmark.kind = 'bookmark'"
        if bookmarked else ""
    )
    marked_bad_join = (
        "JOIN message_annotations ma_bad ON ma_bad.msg_id = m.id AND ma_bad.kind = 'bad_response'"
        if marked_bad else ""
    )
    if topic:
        where += " AND m.topic = ?"
        params.append(topic)
    if agent:
        where += " AND m.agent = ?"
        params.append(agent)
    if adhoc is not None:
        where += " AND COALESCE(m.adhoc, 0) = ?"
        params.append(1 if adhoc else 0)
    if flow_route:
        clause, clause_params = _flow_route_filter_clause(flow_route)
        where += f" AND {clause}"
        params.extend(clause_params)

    with _connect() as conn:
        total = conn.execute(
            f"""SELECT COUNT(*) FROM chat_messages m
                {bookmark_join}
                {marked_bad_join}
                {where}""",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                       m.content, m.status, m.source, m.adhoc, m.session_id,
                       m.flow_run_id, m.flow_route,
                       m.context, m.status_raw, m.created_at AS timestamp,
                       {_turn_end_expr("m")} AS completed_at, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context, u.source AS prompt_source,
                       m.session_turn_index AS session_turn_count,
                       re.payload AS stats_payload,
                       {_marked_bad_expr("m")} AS marked_bad
                FROM chat_messages m
                {bookmark_join}
                {marked_bad_join}
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                {_latest_stats_event_join(msg_alias="m", outer=True)}
                {where}
                ORDER BY completed_at DESC, m.id DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        raw_items = [dict(r) for r in rows]
        for row in raw_items:
            row["flow_route"] = _infer_origin_flow_route(conn, row)

    items = []
    for row in raw_items:
        _attach_turn_stats(row)
        items.append(row)

    return {
        "items": items,
        "total": total,
        "has_more": (offset + limit) < total,
    }


def _history_filter_sql(topic: Optional[str] = None, agent: Optional[str] = None,
                        adhoc: Optional[bool] = None, flow_route: Optional[str] = None,
                        bookmarked: bool = False, marked_bad: bool = False) -> tuple[str, list, str, str]:
    where = "WHERE m.role = 'assistant'"
    params: list = []
    bookmark_join = (
        "JOIN message_annotations ma_bookmark ON ma_bookmark.msg_id = m.id AND ma_bookmark.kind = 'bookmark'"
        if bookmarked else ""
    )
    marked_bad_join = (
        "JOIN message_annotations ma_bad ON ma_bad.msg_id = m.id AND ma_bad.kind = 'bad_response'"
        if marked_bad else ""
    )
    if topic:
        where += " AND m.topic = ?"
        params.append(topic)
    if agent:
        where += " AND m.agent = ?"
        params.append(agent)
    if adhoc is not None:
        where += " AND COALESCE(m.adhoc, 0) = ?"
        params.append(1 if adhoc else 0)
    if flow_route:
        clause, clause_params = _flow_route_filter_clause(flow_route)
        where += f" AND {clause}"
        params.extend(clause_params)
    return where, params, bookmark_join, marked_bad_join


def _history_rows(conn: sqlite3.Connection, where: str, params: list,
                  bookmark_join: str = "", marked_bad_join: str = "",
                  order_sql: str = "ORDER BY completed_at DESC, m.id DESC",
                  limit: int = 20) -> list[dict]:
    rows = conn.execute(
        f"""SELECT m.id, m.role, m.topic, m.agent,
                   m.content, m.status, m.source, m.adhoc, m.session_id,
                   m.flow_run_id, m.flow_route,
                   m.context, m.status_raw, m.created_at AS timestamp,
                   {_turn_end_expr("m")} AS completed_at, m.reply_to,
                   m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                   u.content AS prompt, u.context AS prompt_context, u.source AS prompt_source,
                   m.session_turn_index AS session_turn_count,
                   re.payload AS stats_payload,
                   {_marked_bad_expr("m")} AS marked_bad
            FROM chat_messages m
            {bookmark_join}
            {marked_bad_join}
            LEFT JOIN chat_messages u ON m.reply_to = u.id
            {_latest_stats_event_join(msg_alias="m", outer=True)}
            {where}
            {order_sql}
            LIMIT ?""",
        params + [limit],
    ).fetchall()
    items = []
    for r in rows:
        row = dict(r)
        row["flow_route"] = _infer_origin_flow_route(conn, row)
        _attach_turn_stats(row)
        items.append(row)
    return items


def _history_cursor(item: Optional[dict]) -> Optional[dict]:
    if not item:
        return None
    return {"completed_at": item.get("completed_at"), "id": item.get("id")}


def get_messages_around(msg_id: int, before: int = 20, after: int = 20,
                        topic: Optional[str] = None, agent: Optional[str] = None,
                        adhoc: Optional[bool] = None, flow_route: Optional[str] = None,
                        bookmarked: bool = False, marked_bad: bool = False) -> dict:
    before = max(0, min(int(before), 100))
    after = max(0, min(int(after), 100))
    where, params, bookmark_join, marked_bad_join = _history_filter_sql(
        topic=topic, agent=agent, adhoc=adhoc, flow_route=flow_route,
        bookmarked=bookmarked, marked_bad=marked_bad,
    )
    with _connect() as conn:
        target_rows = _history_rows(
            conn,
            where + " AND m.id = ?",
            params + [msg_id],
            bookmark_join,
            marked_bad_join,
            limit=1,
        )
        if not target_rows:
            return {"items": [], "target_id": msg_id, "found": False, "has_older": False, "has_newer": False}
        target = target_rows[0]
        end_at = target["completed_at"]
        older_rows = _history_rows(
            conn,
            where + f" AND ({_turn_end_expr('m')} < ? OR ({_turn_end_expr('m')} = ? AND m.id < ?))",
            params + [end_at, end_at, msg_id],
            bookmark_join,
            marked_bad_join,
            limit=before + 1,
        )
        newer_ascending = _history_rows(
            conn,
            where + f" AND ({_turn_end_expr('m')} > ? OR ({_turn_end_expr('m')} = ? AND m.id > ?))",
            params + [end_at, end_at, msg_id],
            bookmark_join,
            marked_bad_join,
            order_sql="ORDER BY completed_at ASC, m.id ASC",
            limit=after + 1,
        )

    has_older = len(older_rows) > before
    has_newer = len(newer_ascending) > after
    older_rows = older_rows[:before]
    newer_rows = list(reversed(newer_ascending[:after]))
    items = newer_rows + [target] + older_rows
    chronological = list(reversed(items))
    return {
        "items": items,
        "target_id": msg_id,
        "found": True,
        "has_older": has_older,
        "has_newer": has_newer,
        "older_cursor": _history_cursor(chronological[0] if chronological else None),
        "newer_cursor": _history_cursor(chronological[-1] if chronological else None),
    }


def get_messages_around_flow(flow_run_id: str, before: int = 20, after: int = 20,
                             topic: Optional[str] = None, agent: Optional[str] = None,
                             adhoc: Optional[bool] = None, flow_route: Optional[str] = None,
                             bookmarked: bool = False, marked_bad: bool = False) -> dict:
    flow_run_id = str(flow_run_id or "").strip()
    if not flow_run_id:
        return {"items": [], "flow_run_id": flow_run_id, "found": False, "has_older": False, "has_newer": False}
    where, params, bookmark_join, marked_bad_join = _history_filter_sql(
        topic=topic, agent=agent, adhoc=adhoc, flow_route=flow_route,
        bookmarked=bookmarked, marked_bad=marked_bad,
    )
    with _connect() as conn:
        anchors = _history_rows(
            conn,
            where + " AND m.flow_run_id = ?",
            params + [flow_run_id],
            bookmark_join,
            marked_bad_join,
            order_sql="ORDER BY m.id ASC",
            limit=1,
        )
    if not anchors:
        return {"items": [], "flow_run_id": flow_run_id, "found": False, "has_older": False, "has_newer": False}
    payload = get_messages_around(
        anchors[0]["id"],
        before=before,
        after=after,
        topic=topic,
        agent=agent,
        adhoc=adhoc,
        flow_route=flow_route,
        bookmarked=bookmarked,
        marked_bad=marked_bad,
    )
    payload["flow_run_id"] = flow_run_id
    return payload


def get_messages_from_cursor(direction: str, cursor_completed_at: str, cursor_id: int,
                             limit: int = 20, topic: Optional[str] = None,
                             agent: Optional[str] = None, adhoc: Optional[bool] = None,
                             flow_route: Optional[str] = None, bookmarked: bool = False,
                             marked_bad: bool = False) -> dict:
    limit = max(1, min(int(limit), 100))
    where, params, bookmark_join, marked_bad_join = _history_filter_sql(
        topic=topic, agent=agent, adhoc=adhoc, flow_route=flow_route,
        bookmarked=bookmarked, marked_bad=marked_bad,
    )
    if direction == "newer":
        cursor_where = where + f" AND ({_turn_end_expr('m')} > ? OR ({_turn_end_expr('m')} = ? AND m.id > ?))"
        order_sql = "ORDER BY completed_at ASC, m.id ASC"
    else:
        direction = "older"
        cursor_where = where + f" AND ({_turn_end_expr('m')} < ? OR ({_turn_end_expr('m')} = ? AND m.id < ?))"
        order_sql = "ORDER BY completed_at DESC, m.id DESC"
    with _connect() as conn:
        rows = _history_rows(
            conn,
            cursor_where,
            params + [cursor_completed_at, cursor_completed_at, cursor_id],
            bookmark_join,
            marked_bad_join,
            order_sql=order_sql,
            limit=limit + 1,
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = list(reversed(rows)) if direction == "newer" else rows
    chronological = list(reversed(items))
    return {
        "items": items,
        "has_more": has_more,
        "direction": direction,
        "older_cursor": _history_cursor(chronological[0] if chronological else None),
        "newer_cursor": _history_cursor(chronological[-1] if chronological else None),
    }


def get_history_items_by_ids(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                       m.content, m.status, m.source, m.adhoc, m.session_id,
                       m.flow_run_id, m.flow_route,
                       m.context, m.status_raw, m.created_at AS timestamp,
                       {_turn_end_expr("m")} AS completed_at, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context, u.source AS prompt_source,
                       m.session_turn_index AS session_turn_count,
                       re.payload AS stats_payload,
                       {_marked_bad_expr("m")} AS marked_bad
                FROM chat_messages m
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                {_latest_stats_event_join(msg_alias="m", outer=True)}
                WHERE m.id IN ({placeholders})
                ORDER BY m.id ASC""",
            ids,
        ).fetchall()
    items = []
    for r in rows:
        row = dict(r)
        _attach_turn_stats(row)
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
                    bookmarked: bool = False, flow_route: Optional[str] = None,
                    marked_bad: bool = False) -> dict:
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
    if flow_route:
        clause, clause_params = _flow_route_filter_clause(flow_route)
        where_parts.append(clause)
        params.extend(clause_params)

    where = "WHERE " + " AND ".join(where_parts)
    bookmark_join = (
        "JOIN message_annotations ma_bookmark ON ma_bookmark.msg_id = m.id AND ma_bookmark.kind = 'bookmark'"
        if bookmarked else ""
    )
    marked_bad_join = (
        "JOIN message_annotations ma_bad ON ma_bad.msg_id = m.id AND ma_bad.kind = 'bad_response'"
        if marked_bad else ""
    )

    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                       m.content, m.status, m.source, m.adhoc, m.session_id,
                       m.flow_run_id, m.flow_route,
                       m.context, m.status_raw, m.created_at AS timestamp,
                       {_turn_end_expr("m")} AS completed_at, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context, u.source AS prompt_source,
                       m.session_turn_index AS session_turn_count,
                       re.payload AS stats_payload,
                       {_marked_bad_expr("m")} AS marked_bad
                FROM chat_messages m
                {bookmark_join}
                {marked_bad_join}
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                {_latest_stats_event_join(msg_alias="m", outer=True)}
                {where}
                ORDER BY m.id DESC LIMIT ?""",
            params + [limit],
        ).fetchall()

    items = []
    for r in rows:
        row = dict(r)
        _attach_turn_stats(row)
        items.append(row)

    return {"items": items}


def search_prompts(q: str, topic: Optional[str] = None, agent: Optional[str] = None,
                   adhoc: Optional[bool] = None, limit: int = 100,
                   bookmarked: bool = False, flow_route: Optional[str] = None,
                   marked_bad: bool = False) -> dict:
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
    if flow_route:
        clause, clause_params = _flow_route_filter_clause(flow_route)
        where_parts.append(clause)
        params.extend(clause_params)

    where = "WHERE " + " AND ".join(where_parts)
    bookmark_join = (
        "JOIN message_annotations ma_bookmark ON ma_bookmark.msg_id = m.id AND ma_bookmark.kind = 'bookmark'"
        if bookmarked else ""
    )
    marked_bad_join = (
        "JOIN message_annotations ma_bad ON ma_bad.msg_id = m.id AND ma_bad.kind = 'bad_response'"
        if marked_bad else ""
    )

    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                       m.content, m.status, m.source, m.adhoc, m.session_id,
                       m.flow_run_id, m.flow_route,
                       m.context, m.status_raw, m.created_at AS timestamp,
                       {_turn_end_expr("m")} AS completed_at, m.reply_to,
                       m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                       u.content AS prompt, u.context AS prompt_context, u.source AS prompt_source,
                       m.session_turn_index AS session_turn_count,
                       re.payload AS stats_payload,
                       {_marked_bad_expr("m")} AS marked_bad
                FROM chat_messages m
                {bookmark_join}
                {marked_bad_join}
                LEFT JOIN chat_messages u ON m.reply_to = u.id
                {_latest_stats_event_join(msg_alias="m", outer=True)}
                {where}
                ORDER BY m.id DESC LIMIT ?""",
            params + [limit],
        ).fetchall()

    items = []
    for r in rows:
        row = dict(r)
        _attach_turn_stats(row)
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
                          u.flow_route,
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
                     AND COALESCE(u.source, 'human') = 'human'
                     AND u.content IS NOT NULL
                     AND TRIM(u.content) != ''
               ), latest_unique AS (
                   SELECT MAX(id) AS id
                   FROM routed_prompts
                   GROUP BY content, COALESCE(flow_route, ''), topic, agent, adhoc
               )
               SELECT p.content, p.topic, p.agent, p.adhoc, p.lookback, p.flow_route
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
        flow_route = (row.get('flow_route') or '').strip()
        prefix = ''
        if flow_route:
            prefix = flow_route + ' '
        elif topic and (topic != 'default' or agent):
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
    harness: Optional[str] = None,
    provider: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    lookback: int = 0,
) -> None:
    if not harness and backend:
        harness, provider = split_agent_ref(backend, provider)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO session_stats
                   (session_id, topic, agent, harness, provider, model, cwd,
                    input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, history_input_tokens,
                    cost_usd, duration_ms, adhoc, lookback, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(session_id) DO UPDATE SET
                   created_at           = CURRENT_TIMESTAMP,
                   topic                = COALESCE(excluded.topic, topic),
                   agent                = COALESCE(excluded.agent, agent),
                   harness              = COALESCE(excluded.harness, harness),
                   provider             = COALESCE(excluded.provider, provider),
                   model                = COALESCE(excluded.model, model),
                   cwd                  = COALESCE(excluded.cwd, cwd),
                   input_tokens         = excluded.input_tokens,
                   output_tokens        = excluded.output_tokens,
                   cache_read_tokens    = excluded.cache_read_tokens,
                   cache_write_tokens   = excluded.cache_write_tokens,
                   history_input_tokens = excluded.history_input_tokens,
                   cost_usd             = excluded.cost_usd,
                   duration_ms          = excluded.duration_ms,
                   adhoc                = excluded.adhoc,
                   lookback             = excluded.lookback""",
            (
                session_id, topic, agent, harness, provider, model, cwd,
                stats.get("input_tokens", 0), stats.get("output_tokens", 0),
                stats.get("cache_read_tokens", 0), stats.get("cache_write_tokens", 0),
                stats.get("history_input_tokens", 0),
                stats.get("cost_usd"), stats.get("duration_ms"),
                1 if stats.get("adhoc") else 0,
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


def _stats_cutoff(days: int, hours: int = 0, anchor: Optional[str] = None) -> Optional[str]:
    if not days and not hours:
        return None
    from datetime import datetime, timedelta, timezone
    end = _stats_anchor_dt(anchor) if anchor else datetime.now(timezone.utc)
    return (end - timedelta(days=days, hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _stats_anchor_dt(anchor: str):
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(anchor.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _append_stats_anchor_upper(clauses: list[str], params: list, column: str, anchor: Optional[str]) -> None:
    # Without an anchor, "now" is an implicit upper bound (no future rows exist).
    # With a past anchor, that assumption breaks, so add an explicit bound.
    if not anchor:
        return
    clauses.append(f"datetime({column}) <= datetime(?)")
    params.append(_stats_anchor_dt(anchor).strftime('%Y-%m-%dT%H:%M:%SZ'))


def _turn_end_expr(alias: str) -> str:
    # Canonical "when did this turn end": current terminal paths persist
    # completed_at on chat_messages. Older rows may predate that column, so use
    # their final stats event before falling back to created_at. Pending rows
    # naturally take the created_at fallback and retain their start position.
    return (
        f"COALESCE("
        f"{alias}.completed_at, "
        f"(SELECT re.created_at FROM run_events re "
        f"WHERE re.msg_id = {alias}.id AND re.event_type = 'stats' "
        f"ORDER BY re.id DESC LIMIT 1), {alias}.created_at)"
    )


def _stats_filter_values(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _append_stats_in_filter(clauses: list[str], params: list, expr: str, values: list[str]) -> None:
    if not values:
        return
    if len(values) == 1:
        clauses.append(f"{expr} = ?")
        params.append(values[0])
        return
    clauses.append(f"{expr} IN ({','.join('?' for _ in values)})")
    params.extend(values)


_STATS_STATUS_VALUES = {"done", "error", "cancelled", "shell"}
_STATS_FLOW_VALUES = {"all", "single", "multi"}


def _stats_status_values(status: str) -> list[str]:
    return [s for s in _stats_filter_values(status) if s in _STATS_STATUS_VALUES]


def _append_stats_status_filter(clauses: list[str], params: list, statuses: list[str], alias: str = "cm") -> None:
    """Filter mutually exclusive UI categories; shell turns have ordinary terminal statuses."""
    if not statuses:
        return
    categories: list[str] = []
    if "shell" in statuses:
        categories.append(f"{alias}.source = 'shell'")
    terminal = [status for status in statuses if status != "shell"]
    if terminal:
        placeholders = ",".join("?" for _ in terminal)
        categories.append(f"({alias}.source != 'shell' AND {alias}.status IN ({placeholders}))")
        params.extend(terminal)
    clauses.append(f"({' OR '.join(categories)})")


def _stats_flow_value(flow: str) -> str:
    return flow if flow in _STATS_FLOW_VALUES else "all"


def _append_stats_flow_filter(clauses: list[str], flow: str, expr: str = "cm.flow_run_id") -> None:
    flow = _stats_flow_value(flow)
    if flow == "single":
        clauses.append(f"{expr} IS NULL")
    elif flow == "multi":
        clauses.append(f"{expr} IS NOT NULL")


def _append_legacy_stats_flow_filter(clauses: list[str], flow: str) -> None:
    if _stats_flow_value(flow) == "multi":
        clauses.append("1 = 0")


_STATS_CHART_AGGS = {"sum", "avg", "min", "max", "p50", "p75", "p95"}
_STATS_TOKENS_IN_EXPR = """CASE
        WHEN COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0) > 0
         AND COALESCE(input_tokens, 0) < COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0)
        THEN COALESCE(input_tokens, 0) + COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0)
        ELSE COALESCE(input_tokens, 0)
    END"""
_STATS_CHART_METRIC_EXPR = {
    "cost": "cost_usd",
    "tokens_in": _STATS_TOKENS_IN_EXPR,
    "tokens_out": "output_tokens",
    "tokens_total": f"({_STATS_TOKENS_IN_EXPR}) + COALESCE(output_tokens, 0)",
    "quota": "CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL THEN quota_after - quota_before ELSE NULL END",
    "duration": "duration_ms / 1000.0",
    "cache_read": "COALESCE(cache_read_tokens, 0)",
    "cache_write": "COALESCE(cache_write_tokens, 0)",
    # Mirrors ui/app.js _splitInputTokens(): the split (Claude-style) case adds
    # cache_write back in since input_tokens there is just the small residual;
    # the non-split (Codex-style) case subtracts cache_read since it's already
    # counted inside input_tokens.
    "new_input": """CASE
        WHEN COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0) > 0
         AND COALESCE(input_tokens, 0) < COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0)
        THEN COALESCE(input_tokens, 0) + COALESCE(cache_write_tokens, 0)
        ELSE MAX(0, COALESCE(input_tokens, 0) - COALESCE(cache_read_tokens, 0))
    END""",
}


def _stats_chart_field(metric: str, agg: str) -> str:
    return f"chart_{metric}_{agg}"


def _cache_hit_components(raw: float, cache_read: float, cache_write: float) -> tuple[float, float]:
    is_split = (cache_read + cache_write) > 0 and raw < (cache_read + cache_write)
    new_input = raw + cache_write if is_split else max(0, raw - cache_read)
    return cache_read, cache_read + new_input


def _stats_payload_metric_value(stats: dict, metric: str) -> Optional[float]:
    def num(key: str) -> float:
        value = stats.get(key)
        return value if isinstance(value, (int, float)) else 0

    if metric == "cost":
        value = stats.get("cost_usd")
        return value if isinstance(value, (int, float)) else None
    if metric == "tokens_out":
        return num("output_tokens")
    if metric == "quota":
        value = stats.get("quota_delta")
        return value if isinstance(value, (int, float)) else None
    if metric == "duration":
        value = stats.get("duration_ms")
        return (value / 1000) if isinstance(value, (int, float)) else None

    raw = num("input_tokens")
    cache_read = num("cache_read_tokens")
    cache_write = num("cache_write_tokens")
    is_split = (cache_read + cache_write) > 0 and raw < (cache_read + cache_write)
    if metric == "tokens_in":
        return raw + cache_read + cache_write if is_split else raw
    if metric == "tokens_total":
        tokens_in = raw + cache_read + cache_write if is_split else raw
        return tokens_in + num("output_tokens")
    if metric == "cache_read":
        return cache_read
    if metric == "cache_write":
        return cache_write
    if metric == "new_input":
        return raw + cache_write if is_split else max(0, raw - cache_read)
    if metric == "cache_hit_rate":
        cache_hits, total = _cache_hit_components(raw, cache_read, cache_write)
        return (cache_hits / total) * 100 if total > 0 else None
    if metric == "avg_tokens_turn":
        return raw + num("output_tokens")
    return None


def _stats_payload_quota_delta(stats: dict, fallback: Optional[float] = None) -> Optional[float]:
    value = stats.get("quota_delta")
    if isinstance(value, (int, float)):
        return value
    before = stats.get("quota_before")
    after = stats.get("quota_after")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return after - before
    return fallback


def _stats_bucket_expr(column: str, period: str, tz_shift: str) -> str:
    if period == "hourly":
        return f"strftime('%Y-%m-%d %H:00', datetime({column}, '{tz_shift}'))"
    if period == "weekly":
        return f"strftime('%Y-%m-%d', datetime({column}, '{tz_shift}'), '-' || strftime('%w', datetime({column}, '{tz_shift}')) || ' days')"
    return f"strftime('%Y-%m-%d', datetime({column}, '{tz_shift}'))"


def _stats_empty_aggregate() -> dict:
    return {
        "sessions": 0,
        "done_turns": 0,
        "error_turns": 0,
        "cancelled_turns": 0,
        "input_tokens": 0,
        "new_input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0,
        "quota_delta": None,
        "duration_values": [],
        "session_turns": 0,
        "adhoc_turns": 0,
        "marked_bad": 0,
        "message_ids": [],
        "chart_values": {},
        "chart_cache_hit": {},
    }


def _stats_add_payload_to_aggregate(
    agg: dict,
    stats: dict,
    *,
    msg_id: Optional[int] = None,
    adhoc: bool = False,
    status: str = "done",
    quota_delta: Optional[float] = None,
    marked_bad: bool = False,
    chart_series: Optional[list[dict]] = None,
) -> None:
    def num(key: str) -> float:
        value = stats.get(key)
        return value if isinstance(value, (int, float)) else 0

    agg["sessions"] += 1
    agg["input_tokens"] += num("input_tokens")
    agg["new_input_tokens"] += num("input_tokens") - num("history_input_tokens")
    agg["output_tokens"] += num("output_tokens")
    agg["cache_read_tokens"] += num("cache_read_tokens")
    agg["cache_write_tokens"] += num("cache_write_tokens")
    cost = stats.get("cost_usd")
    if isinstance(cost, (int, float)):
        agg["cost_usd"] += cost
    qd = _stats_payload_quota_delta(stats, quota_delta)
    if isinstance(qd, (int, float)):
        agg["quota_delta"] = (agg["quota_delta"] or 0) + qd
    duration = stats.get("duration_ms")
    if isinstance(duration, (int, float)):
        agg["duration_values"].append(duration)
    if adhoc:
        agg["adhoc_turns"] += 1
    else:
        agg["session_turns"] += 1
    if status == "cancelled":
        agg["cancelled_turns"] += 1
    elif status == "error":
        agg["error_turns"] += 1
    else:
        agg["done_turns"] += 1
    if msg_id is not None:
        agg["message_ids"].append(str(msg_id))
    if marked_bad:
        agg["marked_bad"] += 1

    chart_metrics = []
    seen_chart_metrics = set()
    for series in chart_series or []:
        metric = str(series.get("metric") or "")
        if not metric or metric in seen_chart_metrics:
            continue
        seen_chart_metrics.add(metric)
        chart_metrics.append(metric)

    for metric in chart_metrics:
        if metric == "cache_hit_rate":
            hits, total = _cache_hit_components(
                num("input_tokens"),
                num("cache_read_tokens"),
                num("cache_write_tokens"),
            )
            if total > 0:
                bucket = agg["chart_cache_hit"].setdefault(metric, {"hits": 0, "total": 0})
                bucket["hits"] += hits
                bucket["total"] += total
            continue
        if metric == "marked_bad":
            value = 1 if marked_bad else 0
        else:
            value = _stats_payload_metric_value(stats, metric)
        if value is not None:
            agg["chart_values"].setdefault(metric, []).append(value)


def _stats_add_turn_count_to_aggregate(
    agg: dict,
    *,
    msg_id: Optional[int],
    adhoc: bool,
    status: str = "done",
    marked_bad: bool = False,
) -> None:
    if adhoc:
        agg["adhoc_turns"] += 1
    else:
        agg["session_turns"] += 1
    if status == "cancelled":
        agg["cancelled_turns"] += 1
    elif status == "error":
        agg["error_turns"] += 1
    else:
        agg["done_turns"] += 1
    if msg_id is not None:
        agg["message_ids"].append(str(msg_id))
    if marked_bad:
        agg["marked_bad"] += 1


def _stats_finalize_aggregate(agg: dict, chart_series: Optional[list[dict]] = None) -> dict:
    result = {
        "sessions": agg["sessions"],
        "input_tokens": agg["input_tokens"],
        "new_input_tokens": agg["new_input_tokens"],
        "output_tokens": agg["output_tokens"],
        "cache_read_tokens": agg["cache_read_tokens"],
        "cache_write_tokens": agg["cache_write_tokens"],
        "cost_usd": agg["cost_usd"],
        "quota_delta": agg["quota_delta"],
        "duration_ms": (
            sum(agg["duration_values"]) / len(agg["duration_values"])
            if agg["duration_values"] else None
        ),
        "done_turns": agg["done_turns"],
        "error_turns": agg["error_turns"],
        "cancelled_turns": agg["cancelled_turns"],
        "session_turns": agg["session_turns"],
        "adhoc_turns": agg["adhoc_turns"],
        "total_turns": agg["session_turns"] + agg["adhoc_turns"],
        "marked_bad": agg["marked_bad"],
        "message_ids": ",".join(agg["message_ids"]) if agg["message_ids"] else None,
    }
    for series in chart_series or []:
        metric = str(series.get("metric") or "")
        agg_name = str(series.get("agg") or "sum").lower()
        if agg_name in _STATS_CHART_AGGS:
            if metric == "cache_hit_rate":
                hit = agg["chart_cache_hit"].get(metric, {})
                total = hit.get("total") or 0
                result[_stats_chart_field(metric, agg_name)] = (
                    (hit.get("hits", 0) / total) * 100 if total > 0 else None
                )
            elif agg_name == "sum" and metric in {"turns", "sessions", "cancelled_turns", "error_turns", "marked_bad"}:
                result[_stats_chart_field(metric, agg_name)] = {
                    "turns": result["total_turns"],
                    "sessions": result["sessions"],
                    "cancelled_turns": result["cancelled_turns"],
                    "error_turns": result["error_turns"],
                    "marked_bad": result["marked_bad"],
                }[metric]
            else:
                result[_stats_chart_field(metric, agg_name)] = _aggregate_values(
                    agg["chart_values"].get(metric, []),
                    agg_name,
                )
    return result


def _latest_stats_event_join(msg_alias: str = "cm", *, outer: bool = False) -> str:
    join_kind = "LEFT JOIN" if outer else "JOIN"
    return f"""{join_kind} run_events re
                ON re.id = (
                    SELECT MAX(re2.id) FROM run_events re2
                    WHERE re2.msg_id = {msg_alias}.id AND re2.event_type = 'stats'
                )"""


def _marked_bad_expr(msg_alias: str = "cm") -> str:
    return (
        "EXISTS ("
        "SELECT 1 FROM message_annotations ma "
        f"WHERE ma.msg_id = {msg_alias}.id AND ma.kind = 'bad_response'"
        ")"
    )


def _attach_turn_stats(row: dict) -> None:
    """Parse the per-turn run_events 'stats' payload (row['stats_payload']) into
    row['stats']. Each turn's own numbers live in run_events keyed by msg_id;
    session_stats is keyed by session_id and only ever holds the latest turn's
    numbers, so it cannot be used here for a specific historical message."""
    payload_raw = row.pop("stats_payload", None)
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    msg_quota_before = row.pop("msg_quota_before", None)
    msg_quota_after = row.pop("msg_quota_after", None)
    quota_delta = row.pop("quota_delta", None)
    if not payload:
        return
    harness = payload.get("harness")
    provider = payload.get("provider")
    row["stats"] = {
        "input_tokens": payload.get("input_tokens"),
        "output_tokens": payload.get("output_tokens"),
        "cache_read_tokens": payload.get("cache_read_tokens"),
        "cache_write_tokens": payload.get("cache_write_tokens"),
        "history_input_tokens": payload.get("history_input_tokens"),
        "cost_usd": payload.get("cost_usd"),
        "duration_ms": payload.get("duration_ms"),
        "lookback": payload.get("lookback"),
        "backend": agent_ref_for_storage(harness, provider) if harness else None,
        "model": payload.get("model"),
        "cwd": payload.get("cwd"),
        "session_id": payload.get("session_id") or row.get("session_id"),
        "quota_delta": quota_delta,
        "msg_quota_before": msg_quota_before,
        "msg_quota_after": msg_quota_after,
    }


def _percentile(values: list[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * percentile))
    return ordered[max(0, min(idx, len(ordered) - 1))]


def _aggregate_values(values: list[float], agg: str) -> Optional[float]:
    values = [v for v in values if v is not None]
    if not values:
        return None
    if agg == "sum":
        return sum(values)
    if agg == "avg":
        return sum(values) / len(values)
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    if agg == "p50":
        return _percentile(values, 0.50)
    if agg == "p75":
        return _percentile(values, 0.75)
    if agg == "p95":
        return _percentile(values, 0.95)
    return None


def _merge_chart_aggregates(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    period: str,
    days: int,
    agent: str,
    topic: str,
    adhoc: str,
    tz_offset_minutes: int,
    chart_series: Optional[list[dict]],
    anchor: Optional[str] = None,
) -> None:
    if not rows or not chart_series:
        return
    tz_shift = f"{-tz_offset_minutes} minutes"
    if period == "hourly":
        bucket = f"strftime('%Y-%m-%d %H:00', datetime(created_at, '{tz_shift}'))"
    elif period == "weekly":
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'), '-' || strftime('%w', datetime(created_at, '{tz_shift}')) || ' days')"
    else:
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'))"
    cutoff = _stats_cutoff(days, anchor=anchor)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)
    clauses: list[str] = ["created_at IS NOT NULL"]
    params: list = []
    if cutoff:
        clauses.append("datetime(created_at) >= datetime(?)")
        params.append(cutoff)
    _append_stats_anchor_upper(clauses, params, "created_at", anchor)
    _append_stats_in_filter(clauses, params, _stats_agent_expr(), agents)
    _append_stats_in_filter(clauses, params, "topic", topics)
    if adhoc == "session":
        clauses.append("COALESCE(adhoc, 0) = 0")
    elif adhoc == "adhoc":
        clauses.append("COALESCE(adhoc, 0) = 1")
    where = " AND ".join(clauses)

    cm_clauses: list[str] = ["cm.role = 'assistant'", "cm.status IN ('done', 'error', 'cancelled')"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(re.created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_anchor_upper(cm_clauses, cm_params, "re.created_at", anchor)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.agent", agents)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.topic", topics)
    if adhoc == "session":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 1")
    cm_where = " AND ".join(cm_clauses)

    count_clauses: list[str] = ["cm.role = 'assistant'", "cm.status IN ('done', 'error', 'cancelled')"]
    count_params: list = []
    if cutoff:
        count_clauses.append("datetime(cm.created_at) >= datetime(?)")
        count_params.append(cutoff)
    _append_stats_anchor_upper(count_clauses, count_params, "cm.created_at", anchor)
    _append_stats_in_filter(count_clauses, count_params, "cm.agent", agents)
    _append_stats_in_filter(count_clauses, count_params, "cm.topic", topics)
    if adhoc == "session":
        count_clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        count_clauses.append("COALESCE(cm.adhoc, 0) = 1")
    count_where = " AND ".join(count_clauses)
    turn_rows = conn.execute(
        f"""SELECT {bucket.replace('created_at', 're.created_at')} AS period, re.payload,
                   {_marked_bad_expr("cm")} AS marked_bad
            FROM chat_messages cm
            JOIN run_events re
                ON re.id = (
                    SELECT MAX(re2.id) FROM run_events re2
                    WHERE re2.msg_id = cm.id AND re2.event_type = 'stats'
                )
            WHERE {cm_where}""",
        cm_params,
    ).fetchall()

    for series in chart_series:
        metric = str(series.get("metric") or "")
        agg = str(series.get("agg") or "sum").lower()
        expr = _STATS_CHART_METRIC_EXPR.get(metric)
        if agg not in _STATS_CHART_AGGS:
            continue
        field = _stats_chart_field(metric, agg)
        if metric == "cache_hit_rate":
            grouped_hits: dict[str, list[float]] = {}
            grouped_totals: dict[str, list[float]] = {}
            for turn in turn_rows:
                try:
                    stats = json.loads(turn["payload"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                hits, total = _cache_hit_components(
                    float(stats.get("input_tokens") or 0),
                    float(stats.get("cache_read_tokens") or 0),
                    float(stats.get("cache_write_tokens") or 0),
                )
                if total > 0:
                    grouped_hits.setdefault(turn["period"], []).append(hits)
                    grouped_totals.setdefault(turn["period"], []).append(total)
            if not grouped_hits:
                sample_rows = conn.execute(
                    f"""SELECT {bucket} AS period,
                              input_tokens, cache_read_tokens, cache_write_tokens
                        FROM session_stats
                        WHERE {where}""",
                    params,
                ).fetchall()
                for sample in sample_rows:
                    hits, total = _cache_hit_components(
                        float(sample["input_tokens"] or 0),
                        float(sample["cache_read_tokens"] or 0),
                        float(sample["cache_write_tokens"] or 0),
                    )
                    if total > 0:
                        grouped_hits.setdefault(sample["period"], []).append(hits)
                        grouped_totals.setdefault(sample["period"], []).append(total)
            for row in rows:
                hits = sum(grouped_hits.get(row["period"], []))
                total = sum(grouped_totals.get(row["period"], []))
                row[field] = (hits / total) * 100 if total > 0 else None
            continue
        grouped: dict[str, list[float]] = {}
        for turn in turn_rows:
            try:
                stats = json.loads(turn["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if metric == "marked_bad":
                value = 1 if turn["marked_bad"] else 0
            else:
                value = _stats_payload_metric_value(stats, metric)
            if value is not None:
                grouped.setdefault(turn["period"], []).append(value)
        if grouped:
            for row in rows:
                row[field] = _aggregate_values(grouped.get(row["period"], []), agg)
            continue
        if not expr:
            continue
        sample_rows = conn.execute(
            f"""SELECT {bucket} AS period, {expr} AS value
                FROM session_stats
                WHERE {where} AND ({expr}) IS NOT NULL""",
            params,
        ).fetchall()
        grouped: dict[str, list[float]] = {}
        for sample in sample_rows:
            grouped.setdefault(sample["period"], []).append(sample["value"])
        for row in rows:
            row[field] = _aggregate_values(grouped.get(row["period"], []), agg)


def get_aggregated_stats(
    period: str = "daily",
    days: int = 30,
    agent: str = "",
    topic: str = "",
    adhoc: str = "all",
    status: str = "",
    tz_offset_minutes: int = 0,
    chart_series: Optional[list[dict]] = None,
    anchor: Optional[str] = None,
    flow: str = "all",
) -> list:
    # Shift UTC timestamps to local time before bucketing so day boundaries
    # reflect the user's clock, not UTC midnight.
    # getTimezoneOffset() returns minutes to subtract from local to get UTC,
    # so negating it gives the offset to add to UTC to get local.
    tz_shift = f"{-tz_offset_minutes} minutes"
    re_bucket = _stats_bucket_expr("re.created_at", period, tz_shift)
    cm_time_expr = "COALESCE(cm.completed_at, cm.created_at)"
    cm_bucket = _stats_bucket_expr(cm_time_expr, period, tz_shift)
    ss_bucket = _stats_bucket_expr("ss.created_at", period, tz_shift)
    if not days:
        limit = 5000
    elif period == "hourly":
        limit = days * 24 + 1
    elif period == "weekly":
        limit = max(days // 7, 1) + 1
    else:
        limit = days + 1
    cutoff = _stats_cutoff(days, anchor=anchor)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)
    statuses = _stats_status_values(status)

    cm_clauses: list[str] = ["cm.role = 'assistant'", "cm.status IN ('done', 'error', 'cancelled')"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(re.created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_anchor_upper(cm_clauses, cm_params, "re.created_at", anchor)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.agent", agents)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.topic", topics)
    _append_stats_status_filter(cm_clauses, cm_params, statuses)

    if adhoc == "session":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 1")
    _append_stats_flow_filter(cm_clauses, flow)
    cm_where = " AND ".join(cm_clauses)

    count_clauses: list[str] = ["cm.role = 'assistant'", "cm.status IN ('done', 'error', 'cancelled')"]
    count_params: list = []
    if cutoff:
        count_clauses.append("datetime(COALESCE(cm.completed_at, cm.created_at)) >= datetime(?)")
        count_params.append(cutoff)
    _append_stats_anchor_upper(count_clauses, count_params, "COALESCE(cm.completed_at, cm.created_at)", anchor)
    _append_stats_in_filter(count_clauses, count_params, "cm.agent", agents)
    _append_stats_in_filter(count_clauses, count_params, "cm.topic", topics)
    _append_stats_status_filter(count_clauses, count_params, statuses)
    if adhoc == "session":
        count_clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        count_clauses.append("COALESCE(cm.adhoc, 0) = 1")
    _append_stats_flow_filter(count_clauses, flow)
    count_where = " AND ".join(count_clauses)

    ss_clauses: list[str] = ["ss.created_at IS NOT NULL"]
    ss_params: list = []
    if statuses and "done" not in statuses:
        ss_clauses.append("1 = 0")
    if cutoff:
        ss_clauses.append("datetime(ss.created_at) >= datetime(?)")
        ss_params.append(cutoff)
    _append_stats_anchor_upper(ss_clauses, ss_params, "ss.created_at", anchor)
    _append_stats_in_filter(ss_clauses, ss_params, _stats_agent_expr("ss."), agents)
    _append_stats_in_filter(ss_clauses, ss_params, "ss.topic", topics)
    if adhoc == "session":
        ss_clauses.append("COALESCE(ss.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        ss_clauses.append("COALESCE(ss.adhoc, 0) = 1")
    _append_legacy_stats_flow_filter(ss_clauses, flow)
    ss_where = " AND ".join(ss_clauses)

    try:
        with _connect() as conn:
            event_rows = conn.execute(
                f"""SELECT {re_bucket} AS period, cm.id AS msg_id,
                          COALESCE(cm.adhoc, 0) AS adhoc, cm.status, cm.quota_delta, re.payload,
                          {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    {_latest_stats_event_join()}
                    WHERE {cm_where}""",
                cm_params,
            ).fetchall()
            legacy_rows = conn.execute(
                f"""SELECT {ss_bucket} AS period, ss.*
                    FROM session_stats ss
                    WHERE {ss_where}
                      AND NOT EXISTS (
                        SELECT 1
                        FROM chat_messages cm
                        JOIN run_events re
                          ON re.id = (
                            SELECT MAX(re2.id) FROM run_events re2
                            WHERE re2.msg_id = cm.id AND re2.event_type = 'stats'
                          )
                        WHERE cm.session_id = ss.session_id
                      )""",
                ss_params,
            ).fetchall()
            count_rows = conn.execute(
                f"""SELECT {cm_bucket} AS period, cm.id AS msg_id,
                          COALESCE(cm.adhoc, 0) AS adhoc, cm.status,
                          {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    WHERE {count_where}
                      AND NOT EXISTS (
                        SELECT 1 FROM run_events re
                        WHERE re.msg_id = cm.id AND re.event_type = 'stats'
                      )""",
                count_params,
            ).fetchall()

            grouped: dict[str, dict] = {}
            for event in event_rows:
                try:
                    stats = json.loads(event["payload"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    stats = {}
                agg = grouped.setdefault(event["period"], _stats_empty_aggregate())
                _stats_add_payload_to_aggregate(
                    agg, stats,
                    msg_id=event["msg_id"],
                    adhoc=bool(event["adhoc"]),
                    status=event["status"],
                    quota_delta=event["quota_delta"],
                    marked_bad=bool(event["marked_bad"]),
                    chart_series=chart_series,
                )
            for count in count_rows:
                _stats_add_turn_count_to_aggregate(
                    grouped.setdefault(count["period"], _stats_empty_aggregate()),
                    msg_id=count["msg_id"],
                    adhoc=bool(count["adhoc"]),
                    status=count["status"],
                    marked_bad=bool(count["marked_bad"]),
                )
            for legacy in legacy_rows:
                stats = dict(legacy)
                agg = grouped.setdefault(legacy["period"], _stats_empty_aggregate())
                qd = None
                if legacy["quota_before"] is not None and legacy["quota_after"] is not None:
                    qd = legacy["quota_after"] - legacy["quota_before"]
                _stats_add_payload_to_aggregate(
                    agg, stats,
                    adhoc=bool(legacy["adhoc"]),
                    status="done",
                    quota_delta=qd,
                    chart_series=chart_series,
                )
            result = [
                {"period": key, **_stats_finalize_aggregate(agg, chart_series)}
                for key, agg in grouped.items()
            ]
            result.sort(key=lambda r: r["period"], reverse=True)
            result = result[:limit]
        return result
    except sqlite3.OperationalError:
        return []


def get_stats_by_turn(
    days: int = 30, hours: int = 0, agent: str = "", topic: str = "", adhoc: str = "all",
    status: str = "", limit: int = 2000, anchor: Optional[str] = None, flow: str = "all",
) -> list:
    # One row per completed turn, sourced from run_events rather than session_stats:
    # session_stats is keyed by session_id and gets overwritten on every resumed
    # turn of a multi-turn conversation, so it only ever holds the *latest* turn's
    # numbers. The per-turn "stats" event recorded in run_events at completion time
    # is the only place a given turn's own duration/cost/tokens survive later turns.
    cutoff = _stats_cutoff(days, hours, anchor=anchor)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)
    statuses = _stats_status_values(status)

    turn_time_expr = "COALESCE(re.created_at, cm.completed_at, cm.created_at)"
    clauses: list[str] = ["cm.role = 'assistant'", "cm.status IN ('done', 'error', 'cancelled')"]
    params: list = []
    if cutoff:
        clauses.append(f"datetime({turn_time_expr}) >= datetime(?)")
        params.append(cutoff)
    _append_stats_anchor_upper(clauses, params, turn_time_expr, anchor)
    _append_stats_in_filter(clauses, params, "cm.agent", agents)
    _append_stats_in_filter(clauses, params, "cm.topic", topics)
    _append_stats_status_filter(clauses, params, statuses)
    if adhoc == "session":
        clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        clauses.append("COALESCE(cm.adhoc, 0) = 1")
    _append_stats_flow_filter(clauses, flow)
    where = " AND ".join(clauses)

    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""SELECT cm.id AS msg_id, {turn_time_expr} AS period, cm.topic,
                           cm.agent, cm.adhoc, cm.status, cm.quota_delta, re.payload,
                           {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    LEFT JOIN run_events re
                        ON re.id = (
                            SELECT MAX(re2.id) FROM run_events re2
                            WHERE re2.msg_id = cm.id AND re2.event_type = 'stats'
                        )
                    WHERE {where}
                    ORDER BY datetime({turn_time_expr}) DESC, COALESCE(re.id, cm.id) DESC
                    LIMIT ?""",
                (*params, limit),
            ).fetchall()
    except sqlite3.OperationalError:
        return []

    result = []
    for r in rows:
        row = dict(r)
        payload = row.pop("payload")
        try:
            stats = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            stats = {}
        row["sessions"] = 1
        row["total_turns"] = 1
        row["done_turns"] = 1 if row.get("status") == "done" else 0
        row["error_turns"] = 1 if row.get("status") == "error" else 0
        row["cancelled_turns"] = 1 if row.get("status") == "cancelled" else 0
        row["marked_bad"] = 1 if row.get("marked_bad") else 0
        row["message_ids"] = [row["msg_id"]]
        row["input_tokens"] = stats.get("input_tokens") or 0
        row["output_tokens"] = stats.get("output_tokens") or 0
        row["cache_read_tokens"] = stats.get("cache_read_tokens") or 0
        row["cache_write_tokens"] = stats.get("cache_write_tokens") or 0
        row["cost_usd"] = stats.get("cost_usd")
        row["duration_ms"] = stats.get("duration_ms")
        result.append(row)
    return result


def _breakdown_chart_group_key(row: dict, *, include_topic: bool, include_session: bool) -> tuple:
    key = [row.get("period")]
    if include_topic:
        key.append(row.get("topic"))
    key.append(row.get("agent_key"))
    if include_session:
        key.append(row.get("session_type"))
    return tuple(key)


def _merge_breakdown_chart_aggregates(
    conn: sqlite3.Connection,
    rows: list[dict],
    *,
    period: str,
    days: int,
    agent: str,
    topic: str,
    adhoc: str,
    flow: str,
    tz_offset_minutes: int,
    chart_series: Optional[list[dict]],
    breakdown: str,
    anchor: Optional[str] = None,
) -> None:
    if not rows or not chart_series:
        return
    tz_shift = f"{-tz_offset_minutes} minutes"
    if period == "hourly":
        bucket = f"strftime('%Y-%m-%d %H:00', datetime(created_at, '{tz_shift}'))"
    elif period == "weekly":
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'), '-' || strftime('%w', datetime(created_at, '{tz_shift}')) || ' days')"
    else:
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'))"
    include_topic = breakdown in {"topic_agent", "topic_agent_session"}
    include_session = breakdown in {"agent_session", "topic_agent_session"}
    base_agent = _stats_agent_expr()
    agent_key_expr = base_agent
    if include_session:
        agent_key_expr = f"{base_agent} || CASE WHEN COALESCE(adhoc, 0) = 1 THEN '!' ELSE '' END"
    session_type = "CASE WHEN COALESCE(adhoc, 0) = 1 THEN 'adhoc' ELSE 'session' END"

    select_dims = [f"{bucket} AS period"]
    group_dims = ["period", "agent_key"]
    if include_topic:
        select_dims.append("topic")
        group_dims.append("topic")
    select_dims.extend([f"{agent_key_expr} AS agent_key", f"{agent_key_expr} AS agent"])
    if include_session:
        select_dims.append(f"{session_type} AS session_type")
        group_dims.append("session_type")

    cutoff = _stats_cutoff(days, anchor=anchor)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)
    clauses: list[str] = ["created_at IS NOT NULL"]
    params: list = []
    if cutoff:
        clauses.append("datetime(created_at) >= datetime(?)")
        params.append(cutoff)
    _append_stats_anchor_upper(clauses, params, "created_at", anchor)
    _append_stats_in_filter(clauses, params, _stats_agent_expr(), agents)
    _append_stats_in_filter(clauses, params, "topic", topics)
    if adhoc == "session":
        clauses.append("COALESCE(adhoc, 0) = 0")
    elif adhoc == "adhoc":
        clauses.append("COALESCE(adhoc, 0) = 1")
    _append_legacy_stats_flow_filter(clauses, flow)
    where = " AND ".join(clauses)

    for series in chart_series:
        metric = str(series.get("metric") or "")
        agg = str(series.get("agg") or "sum").lower()
        expr = _STATS_CHART_METRIC_EXPR.get(metric)
        if agg not in _STATS_CHART_AGGS:
            continue
        field = _stats_chart_field(metric, agg)
        if metric == "cache_hit_rate":
            sample_rows = conn.execute(
                f"""SELECT {', '.join(select_dims)},
                          input_tokens, cache_read_tokens, cache_write_tokens
                    FROM session_stats
                    WHERE {where}""",
                params,
            ).fetchall()
            grouped_hits: dict[tuple, list[float]] = {}
            grouped_totals: dict[tuple, list[float]] = {}
            for sample in sample_rows:
                sample_dict = dict(sample)
                hits, total = _cache_hit_components(
                    float(sample_dict.get("input_tokens") or 0),
                    float(sample_dict.get("cache_read_tokens") or 0),
                    float(sample_dict.get("cache_write_tokens") or 0),
                )
                if total > 0:
                    key = _breakdown_chart_group_key(
                        sample_dict, include_topic=include_topic, include_session=include_session
                    )
                    grouped_hits.setdefault(key, []).append(hits)
                    grouped_totals.setdefault(key, []).append(total)
            for row in rows:
                key = _breakdown_chart_group_key(row, include_topic=include_topic, include_session=include_session)
                hits = sum(grouped_hits.get(key, []))
                total = sum(grouped_totals.get(key, []))
                row[field] = (hits / total) * 100 if total > 0 else None
            continue
        if not expr:
            continue
        sample_rows = conn.execute(
            f"""SELECT {', '.join(select_dims)}, {expr} AS value
                FROM session_stats
                WHERE {where} AND ({expr}) IS NOT NULL""",
            params,
        ).fetchall()
        grouped: dict[tuple, list[float]] = {}
        for sample in sample_rows:
            sample_dict = dict(sample)
            grouped.setdefault(
                _breakdown_chart_group_key(sample_dict, include_topic=include_topic, include_session=include_session),
                [],
            ).append(sample_dict["value"])
        for row in rows:
            row[field] = _aggregate_values(
                grouped.get(_breakdown_chart_group_key(row, include_topic=include_topic, include_session=include_session), []),
                agg,
            )


def get_stats_by_agent_breakdown(
    period: str = "daily",
    days: int = 30,
    agent: str = "",
    topic: str = "",
    adhoc: str = "all",
    status: str = "",
    tz_offset_minutes: int = 0,
    include_session: bool = False,
    chart_series: Optional[list[dict]] = None,
    flow: str = "all",
) -> list:
    breakdown = "agent_session" if include_session else "agent"
    return get_stats_by_breakdown(
        period, days, agent, topic, adhoc, status, tz_offset_minutes, breakdown, chart_series, flow=flow,
    )


def get_stats_by_breakdown(
    period: str = "daily",
    days: int = 30,
    agent: str = "",
    topic: str = "",
    adhoc: str = "all",
    status: str = "",
    tz_offset_minutes: int = 0,
    breakdown: str = "agent",
    chart_series: Optional[list[dict]] = None,
    anchor: Optional[str] = None,
    flow: str = "all",
) -> list:
    tz_shift = f"{-tz_offset_minutes} minutes"
    re_bucket = _stats_bucket_expr("re.created_at", period, tz_shift)
    cm_time_expr = "COALESCE(cm.completed_at, cm.created_at)"
    cm_bucket = _stats_bucket_expr(cm_time_expr, period, tz_shift)
    ss_bucket = _stats_bucket_expr("ss.created_at", period, tz_shift)
    include_topic = breakdown in {"topic_agent", "topic_agent_session"}
    include_session = breakdown in {"agent_session", "topic_agent_session"}
    base_agent = _stats_agent_expr()
    if not days:
        limit = 5000
    elif period == "hourly":
        limit = days * 24 + 1
    elif period == "weekly":
        limit = max(days // 7, 1) + 1
    else:
        limit = days + 1
    cutoff = _stats_cutoff(days, anchor=anchor)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)
    statuses = _stats_status_values(status)

    cm_clauses: list[str] = ["cm.role = 'assistant'", "cm.status IN ('done', 'error', 'cancelled')", "cm.created_at IS NOT NULL"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(re.created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_anchor_upper(cm_clauses, cm_params, "re.created_at", anchor)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.agent", agents)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.topic", topics)
    _append_stats_status_filter(cm_clauses, cm_params, statuses)

    if adhoc == "session":
        cm_clauses.append("COALESCE(adhoc, 0) = 0")
    elif adhoc == "adhoc":
        cm_clauses.append("COALESCE(adhoc, 0) = 1")
    _append_stats_flow_filter(cm_clauses, flow)

    ss_clauses: list[str] = ["ss.created_at IS NOT NULL"]
    ss_params: list = []
    if statuses and "done" not in statuses:
        ss_clauses.append("1 = 0")
    if cutoff:
        ss_clauses.append("datetime(ss.created_at) >= datetime(?)")
        ss_params.append(cutoff)
    _append_stats_anchor_upper(ss_clauses, ss_params, "ss.created_at", anchor)
    _append_stats_in_filter(ss_clauses, ss_params, _stats_agent_expr("ss."), agents)
    _append_stats_in_filter(ss_clauses, ss_params, "ss.topic", topics)
    if adhoc == "session":
        ss_clauses.append("COALESCE(ss.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        ss_clauses.append("COALESCE(ss.adhoc, 0) = 1")
    _append_legacy_stats_flow_filter(ss_clauses, flow)

    cm_where = " AND ".join(cm_clauses)

    count_clauses: list[str] = ["cm.role = 'assistant'", "cm.status IN ('done', 'error', 'cancelled')", "cm.created_at IS NOT NULL"]
    count_params: list = []
    if cutoff:
        count_clauses.append("datetime(COALESCE(cm.completed_at, cm.created_at)) >= datetime(?)")
        count_params.append(cutoff)
    _append_stats_anchor_upper(count_clauses, count_params, "COALESCE(cm.completed_at, cm.created_at)", anchor)
    _append_stats_in_filter(count_clauses, count_params, "cm.agent", agents)
    _append_stats_in_filter(count_clauses, count_params, "cm.topic", topics)
    _append_stats_status_filter(count_clauses, count_params, statuses)
    if adhoc == "session":
        count_clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        count_clauses.append("COALESCE(cm.adhoc, 0) = 1")
    _append_stats_flow_filter(count_clauses, flow)
    count_where = " AND ".join(count_clauses)
    ss_where = " AND ".join(ss_clauses)

    try:
        with _connect() as conn:
            event_rows = conn.execute(
                f"""SELECT {re_bucket} AS period, cm.id AS msg_id, cm.topic,
                          COALESCE(cm.agent, 'unknown') AS agent,
                          COALESCE(cm.adhoc, 0) AS adhoc, cm.status, cm.quota_delta, re.payload,
                          {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    {_latest_stats_event_join()}
                    WHERE {cm_where}""",
                cm_params,
            ).fetchall()
            legacy_rows = conn.execute(
                f"""SELECT {ss_bucket} AS period, ss.*,
                          {_stats_agent_expr("ss.")} AS agent_label
                    FROM session_stats ss
                    WHERE {ss_where}
                      AND NOT EXISTS (
                        SELECT 1
                        FROM chat_messages cm
                        JOIN run_events re
                          ON re.id = (
                            SELECT MAX(re2.id) FROM run_events re2
                            WHERE re2.msg_id = cm.id AND re2.event_type = 'stats'
                          )
                        WHERE cm.session_id = ss.session_id
                      )""",
                ss_params,
            ).fetchall()
            count_rows = conn.execute(
                f"""SELECT {cm_bucket} AS period, cm.id AS msg_id, cm.topic,
                          COALESCE(cm.agent, 'unknown') AS agent,
                          COALESCE(cm.adhoc, 0) AS adhoc, cm.status,
                          {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    WHERE {count_where}
                      AND NOT EXISTS (
                        SELECT 1 FROM run_events re
                        WHERE re.msg_id = cm.id AND re.event_type = 'stats'
                      )""",
                count_params,
            ).fetchall()

            def key_for(period_value: str, topic_value: Optional[str], agent_value: str, adhoc_value: bool) -> tuple:
                agent_key = f"{agent_value}!" if include_session and adhoc_value else agent_value
                parts: list = [period_value]
                if include_topic:
                    parts.append(topic_value or "unknown")
                parts.append(agent_key)
                if include_session:
                    parts.append("adhoc" if adhoc_value else "session")
                return tuple(parts)

            grouped: dict[tuple, dict] = {}
            dims: dict[tuple, dict] = {}
            for event in event_rows:
                adhoc_value = bool(event["adhoc"])
                key = key_for(event["period"], event["topic"], event["agent"], adhoc_value)
                dims.setdefault(key, {
                    "period": event["period"],
                    **({"topic": event["topic"] or "unknown"} if include_topic else {}),
                    "agent_key": f"{event['agent']}!" if include_session and adhoc_value else event["agent"],
                    "agent": f"{event['agent']}!" if include_session and adhoc_value else event["agent"],
                    **({"session_type": "adhoc" if adhoc_value else "session"} if include_session else {}),
                })
                try:
                    stats = json.loads(event["payload"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    stats = {}
                _stats_add_payload_to_aggregate(
                    grouped.setdefault(key, _stats_empty_aggregate()),
                    stats,
                    msg_id=event["msg_id"],
                    adhoc=adhoc_value,
                    status=event["status"],
                    quota_delta=event["quota_delta"],
                    marked_bad=bool(event["marked_bad"]),
                    chart_series=chart_series,
                )
            for count in count_rows:
                adhoc_value = bool(count["adhoc"])
                key = key_for(count["period"], count["topic"], count["agent"], adhoc_value)
                dims.setdefault(key, {
                    "period": count["period"],
                    **({"topic": count["topic"] or "unknown"} if include_topic else {}),
                    "agent_key": f"{count['agent']}!" if include_session and adhoc_value else count["agent"],
                    "agent": f"{count['agent']}!" if include_session and adhoc_value else count["agent"],
                    **({"session_type": "adhoc" if adhoc_value else "session"} if include_session else {}),
                })
                _stats_add_turn_count_to_aggregate(
                    grouped.setdefault(key, _stats_empty_aggregate()),
                    msg_id=count["msg_id"],
                    adhoc=adhoc_value,
                    status=count["status"],
                    marked_bad=bool(count["marked_bad"]),
                )
            for legacy in legacy_rows:
                adhoc_value = bool(legacy["adhoc"])
                agent_label = legacy["agent_label"] or "unknown"
                key = key_for(legacy["period"], legacy["topic"], agent_label, adhoc_value)
                dims.setdefault(key, {
                    "period": legacy["period"],
                    **({"topic": legacy["topic"] or "unknown"} if include_topic else {}),
                    "agent_key": f"{agent_label}!" if include_session and adhoc_value else agent_label,
                    "agent": f"{agent_label}!" if include_session and adhoc_value else agent_label,
                    **({"session_type": "adhoc" if adhoc_value else "session"} if include_session else {}),
                })
                qd = None
                if legacy["quota_before"] is not None and legacy["quota_after"] is not None:
                    qd = legacy["quota_after"] - legacy["quota_before"]
                _stats_add_payload_to_aggregate(
                    grouped.setdefault(key, _stats_empty_aggregate()),
                    dict(legacy),
                    adhoc=adhoc_value,
                    status="done",
                    quota_delta=qd,
                    chart_series=chart_series,
                )
            result = [
                {**dims[key], **_stats_finalize_aggregate(agg, chart_series)}
                for key, agg in grouped.items()
            ]
            result.sort(key=lambda r: (r["period"], r["sessions"]), reverse=True)
            result = result[:limit * 20]
        return result
    except sqlite3.OperationalError:
        return []


def _stats_preset_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["state"] = json.loads(item.pop("state_json") or "{}")
    item["is_default"] = bool(item.get("is_default"))
    return item


def list_stats_filter_presets() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, name, state_json, is_default, created_at, updated_at
               FROM stats_filter_presets
               ORDER BY display_order, lower(name)"""
        ).fetchall()
    return [_stats_preset_row(row) for row in rows]


def create_stats_filter_preset(name: str, state: dict) -> dict:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO stats_filter_presets(name, state_json, display_order)
               VALUES (?, ?, COALESCE((SELECT MAX(display_order) + 1 FROM stats_filter_presets), 0))""",
            (name.strip(), json.dumps(state, sort_keys=True)),
        )
        row = conn.execute(
            """SELECT id, name, state_json, is_default, created_at, updated_at
               FROM stats_filter_presets WHERE id=?""",
            (cur.lastrowid,),
        ).fetchone()
    return _stats_preset_row(row)


def update_stats_filter_preset(preset_id: int, name: Optional[str] = None, state: Optional[dict] = None, is_default: Optional[bool] = None) -> Optional[dict]:
    with _connect() as conn:
        if is_default is True:
            conn.execute("UPDATE stats_filter_presets SET is_default=0")
        fields: list[str] = []
        params: list = []
        if name is not None:
            fields.append("name=?")
            params.append(name.strip())
        if state is not None:
            fields.append("state_json=?")
            params.append(json.dumps(state, sort_keys=True))
        if is_default is not None:
            fields.append("is_default=?")
            params.append(1 if is_default else 0)
        if fields:
            fields.append("updated_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
            conn.execute(f"UPDATE stats_filter_presets SET {', '.join(fields)} WHERE id=?", (*params, preset_id))
        row = conn.execute(
            """SELECT id, name, state_json, is_default, created_at, updated_at
               FROM stats_filter_presets WHERE id=?""",
            (preset_id,),
        ).fetchone()
    return _stats_preset_row(row) if row else None


def delete_stats_filter_preset(preset_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM stats_filter_presets WHERE id=?", (preset_id,))
        return cur.rowcount > 0


def get_stats_by_agent(
    days: int = 30, agent: str = "", topic: str = "", adhoc: str = "all",
    anchor: Optional[str] = None, flow: str = "all",
) -> list:
    cutoff = _stats_cutoff(days, anchor=anchor)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)

    cm_clauses: list[str] = ["cm.role = 'assistant'"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(re.created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_anchor_upper(cm_clauses, cm_params, "re.created_at", anchor)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.topic", topics)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.agent", agents)

    if adhoc == "session":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 1")
    _append_stats_flow_filter(cm_clauses, flow)

    ss_clauses: list[str] = ["ss.created_at IS NOT NULL"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("datetime(ss.created_at) >= datetime(?)")
        ss_params.append(cutoff)
    _append_stats_anchor_upper(ss_clauses, ss_params, "ss.created_at", anchor)
    _append_stats_in_filter(ss_clauses, ss_params, "ss.topic", topics)
    _append_stats_in_filter(ss_clauses, ss_params, _stats_agent_expr("ss."), agents)
    if adhoc == "session":
        ss_clauses.append("COALESCE(ss.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        ss_clauses.append("COALESCE(ss.adhoc, 0) = 1")
    _append_legacy_stats_flow_filter(ss_clauses, flow)
    cm_where = " AND ".join(cm_clauses)
    ss_where = " AND ".join(ss_clauses)

    try:
        with _connect() as conn:
            event_rows = conn.execute(
                f"""SELECT COALESCE(cm.agent, 'unknown') AS agent,
                          cm.id AS msg_id, COALESCE(cm.adhoc, 0) AS adhoc, cm.status,
                          cm.quota_delta, re.payload, {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    {_latest_stats_event_join()}
                    WHERE {cm_where}""",
                cm_params,
            ).fetchall()
            legacy_rows = conn.execute(
                f"""SELECT ss.*, {_stats_agent_expr("ss.")} AS agent_label
                    FROM session_stats ss
                    WHERE {ss_where}
                      AND NOT EXISTS (
                        SELECT 1 FROM chat_messages cm
                        JOIN run_events re
                          ON re.id = (
                            SELECT MAX(re2.id) FROM run_events re2
                            WHERE re2.msg_id = cm.id AND re2.event_type = 'stats'
                          )
                        WHERE cm.session_id = ss.session_id
                      )""",
                ss_params,
            ).fetchall()
            count_rows = conn.execute(
                f"""SELECT COALESCE(cm.agent, 'unknown') AS agent,
                          cm.id AS msg_id, COALESCE(cm.adhoc, 0) AS adhoc, cm.status,
                          {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    WHERE {cm_where}
                      AND NOT EXISTS (
                        SELECT 1 FROM run_events re
                        WHERE re.msg_id = cm.id AND re.event_type = 'stats'
                      )""",
                cm_params,
            ).fetchall()
            grouped: dict[str, dict] = {}
            for event in event_rows:
                try:
                    stats = json.loads(event["payload"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    stats = {}
                _stats_add_payload_to_aggregate(
                    grouped.setdefault(event["agent"], _stats_empty_aggregate()),
                    stats,
                    msg_id=event["msg_id"],
                    adhoc=bool(event["adhoc"]),
                    status=event["status"],
                    quota_delta=event["quota_delta"],
                    marked_bad=bool(event["marked_bad"]),
                )
            for count in count_rows:
                _stats_add_turn_count_to_aggregate(
                    grouped.setdefault(count["agent"], _stats_empty_aggregate()),
                    msg_id=count["msg_id"],
                    adhoc=bool(count["adhoc"]),
                    status=count["status"],
                    marked_bad=bool(count["marked_bad"]),
                )
            for legacy in legacy_rows:
                qd = None
                if legacy["quota_before"] is not None and legacy["quota_after"] is not None:
                    qd = legacy["quota_after"] - legacy["quota_before"]
                _stats_add_payload_to_aggregate(
                    grouped.setdefault(legacy["agent_label"] or "unknown", _stats_empty_aggregate()),
                    dict(legacy),
                    adhoc=bool(legacy["adhoc"]),
                    status="done",
                    quota_delta=qd,
                )
        result = [{"agent": key, **_stats_finalize_aggregate(agg)} for key, agg in grouped.items()]
        result.sort(key=lambda r: r["sessions"], reverse=True)
        return result
    except sqlite3.OperationalError:
        return []


def get_stats_by_topic(
    days: int = 30, agent: str = "", topic: str = "", adhoc: str = "all",
    anchor: Optional[str] = None, flow: str = "all",
) -> list:
    cutoff = _stats_cutoff(days, anchor=anchor)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)

    cm_clauses: list[str] = ["cm.role = 'assistant'", "cm.status IN ('done', 'error', 'cancelled')"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(re.created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_anchor_upper(cm_clauses, cm_params, "re.created_at", anchor)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.agent", agents)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.topic", topics)

    if adhoc == "session":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 1")
    _append_stats_flow_filter(cm_clauses, flow)

    ss_clauses: list[str] = ["ss.topic IS NOT NULL"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("datetime(ss.created_at) >= datetime(?)")
        ss_params.append(cutoff)
    _append_stats_anchor_upper(ss_clauses, ss_params, "ss.created_at", anchor)
    _append_stats_in_filter(ss_clauses, ss_params, _stats_agent_expr("ss."), agents)
    _append_stats_in_filter(ss_clauses, ss_params, "ss.topic", topics)
    if adhoc == "session":
        ss_clauses.append("COALESCE(ss.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        ss_clauses.append("COALESCE(ss.adhoc, 0) = 1")
    _append_legacy_stats_flow_filter(ss_clauses, flow)
    cm_where = " AND ".join(cm_clauses)
    ss_where = " AND ".join(ss_clauses)

    try:
        with _connect() as conn:
            event_rows = conn.execute(
                f"""SELECT COALESCE(cm.topic, 'unknown') AS topic,
                          cm.id AS msg_id, COALESCE(cm.adhoc, 0) AS adhoc, cm.status,
                          cm.quota_delta, re.payload, {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    {_latest_stats_event_join()}
                    WHERE {cm_where}""",
                cm_params,
            ).fetchall()
            legacy_rows = conn.execute(
                f"""SELECT ss.*
                    FROM session_stats ss
                    WHERE {ss_where}
                      AND NOT EXISTS (
                        SELECT 1 FROM chat_messages cm
                        JOIN run_events re
                          ON re.id = (
                            SELECT MAX(re2.id) FROM run_events re2
                            WHERE re2.msg_id = cm.id AND re2.event_type = 'stats'
                          )
                        WHERE cm.session_id = ss.session_id
                      )""",
                ss_params,
            ).fetchall()
            count_rows = conn.execute(
                f"""SELECT COALESCE(cm.topic, 'unknown') AS topic,
                          cm.id AS msg_id, COALESCE(cm.adhoc, 0) AS adhoc, cm.status,
                          {_marked_bad_expr("cm")} AS marked_bad
                    FROM chat_messages cm
                    WHERE {cm_where}
                      AND NOT EXISTS (
                        SELECT 1 FROM run_events re
                        WHERE re.msg_id = cm.id AND re.event_type = 'stats'
                      )""",
                cm_params,
            ).fetchall()
            grouped: dict[str, dict] = {}
            for event in event_rows:
                try:
                    stats = json.loads(event["payload"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    stats = {}
                _stats_add_payload_to_aggregate(
                    grouped.setdefault(event["topic"], _stats_empty_aggregate()),
                    stats,
                    msg_id=event["msg_id"],
                    adhoc=bool(event["adhoc"]),
                    status=event["status"],
                    quota_delta=event["quota_delta"],
                    marked_bad=bool(event["marked_bad"]),
                )
            for count in count_rows:
                _stats_add_turn_count_to_aggregate(
                    grouped.setdefault(count["topic"], _stats_empty_aggregate()),
                    msg_id=count["msg_id"],
                    adhoc=bool(count["adhoc"]),
                    status=count["status"],
                    marked_bad=bool(count["marked_bad"]),
                )
            for legacy in legacy_rows:
                qd = None
                if legacy["quota_before"] is not None and legacy["quota_after"] is not None:
                    qd = legacy["quota_after"] - legacy["quota_before"]
                _stats_add_payload_to_aggregate(
                    grouped.setdefault(legacy["topic"] or "unknown", _stats_empty_aggregate()),
                    dict(legacy),
                    adhoc=bool(legacy["adhoc"]),
                    status="done",
                    quota_delta=qd,
                )
        result = [{"topic": key, **_stats_finalize_aggregate(agg)} for key, agg in grouped.items()]
        result.sort(key=lambda r: r["sessions"], reverse=True)
        return result
    except sqlite3.OperationalError:
        return []


def get_stats_filter_options() -> dict:
    try:
        with _connect() as conn:
            agents = [
                r[0] for r in conn.execute(
                    f"""SELECT DISTINCT agent_value FROM (
                          SELECT {_stats_agent_expr()} AS agent_value
                          FROM session_stats
                          WHERE agent IS NOT NULL OR harness IS NOT NULL
                          UNION
                          SELECT agent AS agent_value
                          FROM chat_messages
                          WHERE role='assistant' AND agent IS NOT NULL
                            AND status IN ('done', 'error', 'cancelled')
                        ) WHERE agent_value IS NOT NULL ORDER BY 1"""
                ).fetchall() if r[0]
            ]
            topics = [
                r[0] for r in conn.execute(
                    """SELECT DISTINCT topic_value FROM (
                         SELECT topic AS topic_value
                         FROM session_stats
                         WHERE topic IS NOT NULL
                         UNION
                         SELECT topic AS topic_value
                         FROM chat_messages
                         WHERE role='assistant' AND topic IS NOT NULL
                           AND status IN ('done', 'error', 'cancelled')
                       ) WHERE topic_value IS NOT NULL ORDER BY 1"""
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

    A file is revertable when no non-reverted GitDiff that landed later (by
    completed_at, falling back to msg_id when either side's completion time
    is unknown) in the same topic and repo has also touched that file.
    completed_at is the tiebreaker rather than msg_id alone because two turns
    on the same topic can run concurrently and finish out of msg_id order —
    using raw id would call the earlier-finishing (but higher-id) turn
    "later" than a turn that actually landed after it.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT topic, context, completed_at FROM chat_messages WHERE id = ? AND role = 'assistant'",
            (msg_id,),
        ).fetchone()
        if not row or not row['context']:
            return {}

        topic = row['topic']
        this_completed_at = row['completed_at']
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

        candidate_rows = conn.execute(
            """SELECT id, context, completed_at FROM chat_messages
               WHERE topic = ? AND role = 'assistant' AND id != ? AND context IS NOT NULL""",
            (topic, msg_id),
        ).fetchall()

        def _is_later(cand) -> bool:
            cand_completed_at = cand['completed_at']
            if this_completed_at and cand_completed_at and cand_completed_at != this_completed_at:
                return cand_completed_at > this_completed_at
            return cand['id'] > msg_id

        later_rows = [r for r in candidate_rows if _is_later(r)]

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

    omitted = set(this_diff.get('omitted_paths') or [])
    return {
        fpath: (
            'reverted' if fpath in already_reverted
            else 'conflicting' if fpath in later_touched
            else 'diff_too_large' if fpath in omitted
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
            "SELECT id, file_path, before, after, edited_at FROM file_edit_history WHERE file_path = ? ORDER BY id DESC LIMIT ?",
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


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _insert_realtime_event(
    conn: sqlite3.Connection, event_type: str, topic: Optional[str],
    agent: Optional[str], msg_id: Optional[int], run_seq: Optional[int], payload,
) -> int:
    encoded = payload if isinstance(payload, str) else json.dumps(payload or {})
    cur = conn.execute(
        """INSERT INTO realtime_events
           (event_type, topic, agent, msg_id, run_seq, payload)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_type, topic, agent, msg_id, run_seq, encoded),
    )
    event_id = int(cur.lastrowid)
    # Commit the domain mutation and publication row together before waking
    # sockets. The listener may run on a worker thread and must marshal itself
    # onto its owning event loop.
    conn.commit()
    listener = _realtime_commit_listener
    if listener:
        listener(event_id)
    return event_id


def set_realtime_commit_listener(listener: Optional[Callable[[int], None]]) -> None:
    global _realtime_commit_listener
    _realtime_commit_listener = listener


def insert_realtime_event(
    event_type: str, topic: Optional[str], agent: Optional[str],
    payload=None, msg_id: Optional[int] = None, run_seq: Optional[int] = None,
) -> int:
    with _connect() as conn:
        return _insert_realtime_event(conn, event_type, topic, agent, msg_id, run_seq, payload)


def get_realtime_cursor() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM realtime_events").fetchone()[0])


def get_realtime_retained_range() -> tuple[Optional[int], int]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT MIN(event_id), COALESCE(MAX(event_id), 0) FROM realtime_events"
        ).fetchone()
    return (int(row[0]) if row[0] is not None else None, int(row[1]))


def prune_realtime_data(event_days: int = 7, max_events: int = 100_000, request_days: int = 7) -> dict:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM realtime_events WHERE datetime(created_at) < datetime('now', ?)",
            (f"-{max(1, event_days)} days",),
        )
        count = int(conn.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0])
        if count > max_events:
            conn.execute(
                """DELETE FROM realtime_events WHERE event_id <= (
                       SELECT event_id FROM realtime_events ORDER BY event_id DESC LIMIT 1 OFFSET ?
                   )""",
                (max_events,),
            )
        conn.execute(
            "DELETE FROM realtime_requests WHERE datetime(created_at) < datetime('now', ?)",
            (f"-{max(1, request_days)} days",),
        )
        remaining = int(conn.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0])
        requests = int(conn.execute("SELECT COUNT(*) FROM realtime_requests").fetchone()[0])
    return {"events": remaining, "requests": requests}


def get_realtime_events(after_event_id: int, scopes: list[dict], limit: int = 501) -> list[dict]:
    clauses, params = [], [after_event_id]
    for scope in scopes:
        if scope.get("lifecycle") == "global":
            clauses.append("1=1")
            continue
        topic = scope.get("topic")
        agent = scope.get("agent")
        if not topic:
            continue
        if agent is None:
            clauses.append("topic=?")
            params.append(topic)
        else:
            clauses.append("(topic=? AND agent=?)")
            params.extend((topic, agent))
    if not clauses:
        return []
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT event_id, event_type, topic, agent, msg_id, run_seq, payload, created_at
                FROM realtime_events WHERE event_id>? AND ({' OR '.join(clauses)})
                ORDER BY event_id LIMIT ?""",
            params,
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item["payload"])
        except (TypeError, json.JSONDecodeError):
            item["payload"] = {"text": item["payload"] or ""}
        result.append(item)
    return result


def get_realtime_snapshot(scopes: list[dict], message_limit: int = 20) -> dict:
    # Pending rows should normally be few, but stale rows must not turn every
    # reconnect into an unbounded query plus two follow-up queries per row.
    pending_limit = max(1, message_limit)
    conversations = []
    with _connect() as conn:
        cursor = int(conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM realtime_events").fetchone()[0])
        for scope in scopes:
            if scope.get("lifecycle") == "global":
                recent = conn.execute(
                    "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (message_limit,),
                ).fetchall()
                pending = conn.execute(
                    "SELECT * FROM chat_messages WHERE status='pending' ORDER BY id DESC LIMIT ?",
                    (pending_limit,),
                ).fetchall()
                by_id = {int(row["id"]): dict(row) for row in [*recent, *pending]}
                for item in by_id.values():
                    if item["role"] == "assistant" and item["status"] == "pending":
                        snap = _run_event_snapshot(conn, item["id"])
                        item["content"] = snap.get("text") or item.get("content") or ""
                        seq_row = conn.execute(
                            "SELECT COALESCE(MAX(seq), -1) FROM run_events WHERE msg_id=?", (item["id"],),
                        ).fetchone()
                        item["run_seq"] = int(seq_row[0])
                conversations.append({
                    "scope": scope,
                    "messages": sorted(by_id.values(), key=lambda row: row["id"]),
                })
                continue
            topic, agent = scope.get("topic"), scope.get("agent")
            if not topic:
                continue
            agent_clause = "1=1" if agent is None else "agent=?"
            args = [topic] if agent is None else [topic, agent]
            recent = conn.execute(
                f"""SELECT * FROM chat_messages WHERE topic=? AND {agent_clause}
                    ORDER BY id DESC LIMIT ?""", (*args, message_limit),
            ).fetchall()
            pending = conn.execute(
                f"""SELECT * FROM chat_messages WHERE topic=? AND {agent_clause}
                    AND status='pending' ORDER BY id DESC LIMIT ?""", (*args, pending_limit),
            ).fetchall()
            by_id = {int(row["id"]): dict(row) for row in [*recent, *pending]}
            for item in by_id.values():
                if item["role"] == "assistant" and item["status"] == "pending":
                    snap = _run_event_snapshot(conn, item["id"])
                    item["content"] = snap.get("text") or item.get("content") or ""
                    seq_row = conn.execute(
                        "SELECT COALESCE(MAX(seq), -1) FROM run_events WHERE msg_id=?", (item["id"],),
                    ).fetchone()
                    item["run_seq"] = int(seq_row[0])
            conversations.append({"scope": scope, "messages": sorted(by_id.values(), key=lambda row: row["id"])})
    return {"cursor": cursor, "conversations": conversations}


def get_realtime_request(principal: str, request_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT request_type, request_hash, result FROM realtime_requests WHERE principal=? AND request_id=?",
            (principal, request_id),
        ).fetchone()
    if not row:
        return None
    return {"request_type": row["request_type"], "request_hash": row["request_hash"], "result": json.loads(row["result"])}


def save_realtime_request(principal: str, request_id: str, request_type: str, request_hash: str, result: dict) -> dict:
    with _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO realtime_requests
               (principal, request_id, request_type, request_hash, result) VALUES (?, ?, ?, ?, ?)""",
            (principal, request_id, request_type, request_hash, json.dumps(result)),
        )
    return get_realtime_request(principal, request_id)["result"]


def insert_run_event(msg_id: int, seq: int, event_type: str, payload: Optional[str], created_at: Optional[str] = None) -> str:
    event_created_at = created_at or _utc_now_iso()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO run_events (msg_id, seq, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (msg_id, seq, event_type, payload, event_created_at),
        )
        if cur.rowcount == 0:
            logging.getLogger("squid").warning(
                "run_event seq collision: msg_id=%s seq=%s event_type=%s — event silently dropped",
                msg_id, seq, event_type,
            )
            row = conn.execute(
                "SELECT created_at FROM run_events WHERE msg_id=? AND seq=?",
                (msg_id, seq),
            ).fetchone()
            if row and row["created_at"]:
                return row["created_at"]
        else:
            message = conn.execute("SELECT topic, agent FROM chat_messages WHERE id=?", (msg_id,)).fetchone()
            if message:
                event_payload = payload
                if event_type in {"queued", "loading", "processing", "tool", "stats", "diag"} and payload:
                    try:
                        event_payload = json.loads(payload)
                    except json.JSONDecodeError:
                        pass
                elif event_type in {"text", "status", "error"}:
                    event_payload = {"text": payload or ""}
                else:
                    event_payload = {}
                _insert_realtime_event(
                    conn, f"chat.{event_type}", message["topic"], message["agent"],
                    msg_id, seq, event_payload,
                )
    return event_created_at


def _completed_run_snapshot(conn: sqlite3.Connection, msg_id: int) -> Optional[tuple[str, Optional[str]]]:
    rows = conn.execute(
        "SELECT event_type, payload FROM run_events WHERE msg_id=? ORDER BY seq",
        (msg_id,),
    ).fetchall()
    if not any(row["event_type"] == "done" for row in rows):
        return None
    text = "".join(row["payload"] or "" for row in rows if row["event_type"] == "text")
    status_raw = "".join(row["payload"] or "" for row in rows if row["event_type"] == "status")
    return text, _sanitize_status_raw(text, status_raw) or None


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
            """SELECT ma.msg_id AS id, m.topic, m.agent, ma.created_at AS saved_at
               FROM message_annotations ma
               LEFT JOIN chat_messages m ON m.id = ma.msg_id
               WHERE ma.kind = 'bookmark'
               ORDER BY datetime(ma.updated_at) DESC, datetime(ma.created_at) DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def add_bookmark(msg_id: int) -> None:
    set_message_annotation(msg_id, "bookmark")


def remove_bookmark(msg_id: int) -> None:
    remove_message_annotation(msg_id, "bookmark")


# ── message annotations ────────────────────────────────────────────────────────

def get_message_annotations(kind: Optional[str] = None) -> list[dict]:
    with _connect() as conn:
        if kind:
            rows = conn.execute(
                """SELECT msg_id, kind, payload, created_at, updated_at
                   FROM message_annotations
                   WHERE kind=?
                   ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC""",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT msg_id, kind, payload, created_at, updated_at
                   FROM message_annotations
                   ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC"""
            ).fetchall()
    return [dict(r) for r in rows]


def get_message_annotation(msg_id: int, kind: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            """SELECT msg_id, kind, payload, created_at, updated_at
               FROM message_annotations
               WHERE msg_id=? AND kind=?""",
            (msg_id, kind),
        ).fetchone()
    return dict(row) if row else None


def set_message_annotation(
    msg_id: int,
    kind: str,
    payload: Optional[dict] = None,
) -> None:
    payload_text = json.dumps(payload or {}, sort_keys=True)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO message_annotations (msg_id, kind, payload)
               VALUES (?, ?, ?)
               ON CONFLICT(msg_id, kind) DO UPDATE SET
                   payload=excluded.payload,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now')""",
            (msg_id, kind, payload_text),
        )


def remove_message_annotation(msg_id: int, kind: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM message_annotations WHERE msg_id=? AND kind=?",
            (msg_id, kind),
        )


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------

def save_worktree(
    topic: str,
    agent: str,
    repo_root: str,
    wt_path: str,
    br: str,
    base_commit: Optional[str] = None,
    integration_worktree_path: Optional[str] = None,
    status: str = "pending",
) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO worktrees
                 (topic, agent, repo_root, worktree_path, branch_name, base_commit, integration_worktree_path, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(topic, agent, repo_root) DO UPDATE SET
                 base_commit=COALESCE(excluded.base_commit, worktrees.base_commit),
                 integration_worktree_path=COALESCE(excluded.integration_worktree_path, worktrees.integration_worktree_path),
                 last_used_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), status=excluded.status""",
            (topic, agent, repo_root, wt_path, br, base_commit, integration_worktree_path, status),
        )


def mark_worktree_synced(topic: str, agent: str, repo_root: str) -> None:
    """Record that a turn's changes were promoted, without deleting the worktree
    yet — actual removal is a later best-effort sweep (see worktree.cleanup_worktrees),
    so a background process the turn spawned isn't left without its working directory."""
    with _connect() as conn:
        conn.execute(
            """UPDATE worktrees SET status='synced',
                 last_used_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE topic=? AND agent=? AND repo_root=?""",
            (topic, agent, repo_root),
        )


def mark_worktree_status(topic: str, agent: str, repo_root: str, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE worktrees SET status=?,
                 last_used_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE topic=? AND agent=? AND repo_root=?""",
            (status, topic, agent, repo_root),
        )


def get_worktrees(topic: str, agent: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM worktrees WHERE topic=? AND agent=?", (topic, agent)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_worktree(topic: str, agent: str, repo_root: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM worktrees WHERE topic=? AND agent=? AND repo_root=?",
            (topic, agent, repo_root),
        )


def delete_all_worktrees(topic: str, agent: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM worktrees WHERE topic=? AND agent=?", (topic, agent))


def get_all_worktrees_for_topic(topic: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM worktrees WHERE topic=?", (topic,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_all_topic_worktrees(topic: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM worktrees WHERE topic=?", (topic,))
