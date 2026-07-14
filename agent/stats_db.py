"""
stats_db.py — SQLite for stats, chat history, agents, and topics.
"""
import logging
import sqlite3
import json
import os
import re
from pathlib import Path
from typing import Optional

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

# Store database in ~/.squid/ so it persists across installs/updates.
# Override with SQUID_DB_PATH env var (e.g. for containers).
_DB_PATH = Path(os.environ.get("SQUID_DB_PATH", Path.home() / ".squid" / "squid.db"))
_SQUID_WORKTREE_PATH_RE = re.compile(r"(?:~|/[^`'\"<>\s]*)/\.squid/worktrees/[^`'\"<>\s),]+")
_CHANGED_FILES_BLOCK_RE = re.compile(r"\n*Changed files from this response:\n<changed_files>.*?</changed_files>", re.DOTALL)
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
    """CREATE TABLE IF NOT EXISTS worktrees (
        topic           TEXT NOT NULL,
        agent           TEXT NOT NULL,
        repo_root       TEXT NOT NULL,
        worktree_path   TEXT NOT NULL,
        branch_name     TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'active',
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


def init_db() -> None:
    conn = _connect()
    try:
        for ddl in _TABLES:
            conn.execute(ddl)
        agent_columns = _table_columns(conn, "agents")
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
            conn.execute(
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
            conn.execute(
                """INSERT INTO agents (name, harness, provider, model, cwd) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     harness = excluded.harness,
                     provider = excluded.provider,
                     model   = excluded.model,
                     cwd     = excluded.cwd""",
                (name, harness, provider, model, cwd),
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
            conn.execute(
                """INSERT INTO topics (topic, agent, last_prompt, last_adhoc_prompt, last_at, last_model, last_harness, last_provider)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(topic, agent) DO UPDATE SET
                     last_prompt        = COALESCE(excluded.last_prompt, last_prompt),
                     last_adhoc_prompt  = COALESCE(excluded.last_adhoc_prompt, last_adhoc_prompt),
                     last_at            = COALESCE(excluded.last_at, last_at),
                     last_model         = COALESCE(excluded.last_model, last_model),
                     last_harness       = COALESCE(excluded.last_harness, last_harness),
                     last_provider      = COALESCE(excluded.last_provider, last_provider)""",
                (name, agent, session_prompt, adhoc_prompt, at, last_model, last_harness, last_provider),
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
            conn.execute("DELETE FROM topics WHERE topic=? AND agent=?", (topic, agent))
        elif adhoc:
            conn.execute(
                "DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM chat_messages WHERE topic=? AND agent=? AND adhoc=1 AND role='assistant')",
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
    if status == "done":
        status_raw = _sanitize_status_raw(content, status_raw)
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
            f"""SELECT a.id, u.content AS user_content, a.content AS asst_content, a.context
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
            {"role": "user",      "content": row["user_content"]},
            {"role": "assistant", "content": asst_content},
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
    backend_expr = _runtime_ref_expr("s.harness", "s.provider")
    stat_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                 "history_input_tokens", "cost_usd", "duration_ms", "lookback",
                 "quota_before", "quota_after", "quota_delta",
                 "msg_quota_before", "msg_quota_after", "backend", "model", "cwd"}
    with _connect() as conn:
        row = conn.execute(
            f"""SELECT m.id, m.role, m.topic, m.agent,
                      m.content, m.status, m.adhoc, m.session_id,
                      m.context, m.status_raw, m.created_at AS timestamp, m.reply_to,
                      m.quota_delta, m.quota_before AS msg_quota_before, m.quota_after AS msg_quota_after,
                      u.content AS prompt, u.context AS prompt_context,
                      m.session_turn_index AS session_turn_count,
                      s.input_tokens, s.output_tokens, s.cache_read_tokens,
                      s.cache_write_tokens, s.history_input_tokens,
                      s.cost_usd, s.duration_ms, s.lookback,
                      s.quota_before, s.quota_after, {backend_expr} AS backend, s.model, s.cwd
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
    backend_expr = _runtime_ref_expr("s.harness", "s.provider")
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
                       s.quota_before, s.quota_after, {backend_expr} AS backend, s.model, s.cwd
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
    backend_expr = _runtime_ref_expr("s.harness", "s.provider")
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
                       s.quota_before, s.quota_after, {backend_expr} AS backend, s.model, s.cwd
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
    backend_expr = _runtime_ref_expr("s.harness", "s.provider")
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
                       s.quota_before, s.quota_after, {backend_expr} AS backend, s.model, s.cwd
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
    backend_expr = _runtime_ref_expr("s.harness", "s.provider")
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
                       s.quota_before, s.quota_after, {backend_expr} AS backend, s.model, s.cwd
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


def _stats_cutoff(days: int, hours: int = 0) -> Optional[str]:
    if not days and not hours:
        return None
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


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
) -> None:
    if not rows or not chart_series:
        return
    tz_shift = f"{-tz_offset_minutes} minutes"
    if period == "hourly":
        bucket = f"strftime('%Y-%m-%d %H:00', datetime(created_at, '{tz_shift}'))"
    elif period == "weekly":
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'), '-' || ((strftime('%w', datetime(created_at, '{tz_shift}')) + 6) % 7) || ' days')"
    else:
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'))"
    cutoff = _stats_cutoff(days)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)
    clauses: list[str] = ["created_at IS NOT NULL"]
    params: list = []
    if cutoff:
        clauses.append("datetime(created_at) >= datetime(?)")
        params.append(cutoff)
    _append_stats_in_filter(clauses, params, _stats_agent_expr(), agents)
    _append_stats_in_filter(clauses, params, "topic", topics)
    if adhoc == "session":
        clauses.append("COALESCE(adhoc, 0) = 0")
    elif adhoc == "adhoc":
        clauses.append("COALESCE(adhoc, 0) = 1")
    where = " AND ".join(clauses)

    cm_clauses: list[str] = ["cm.role = 'assistant'"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(cm.created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.agent", agents)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.topic", topics)
    if adhoc == "session":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        cm_clauses.append("COALESCE(cm.adhoc, 0) = 1")
    cm_where = " AND ".join(cm_clauses)
    turn_rows = conn.execute(
        f"""SELECT {bucket.replace('created_at', 'cm.created_at')} AS period, re.payload
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
    tz_offset_minutes: int = 0,
    chart_series: Optional[list[dict]] = None,
) -> list:
    # Shift UTC timestamps to local time before bucketing so day boundaries
    # reflect the user's clock, not UTC midnight.
    # getTimezoneOffset() returns minutes to subtract from local to get UTC,
    # so negating it gives the offset to add to UTC to get local.
    tz_shift = f"{-tz_offset_minutes} minutes"
    if period == "hourly":
        ss_bucket = f"strftime('%Y-%m-%d %H:00', datetime(ss_inner.created_at, '{tz_shift}'))"
        cm_bucket = f"strftime('%Y-%m-%d %H:00', datetime(cm.created_at, '{tz_shift}'))"
    elif period == "weekly":
        ss_bucket = f"strftime('%Y-%m-%d', datetime(ss_inner.created_at, '{tz_shift}'), '-' || ((strftime('%w', datetime(ss_inner.created_at, '{tz_shift}')) + 6) % 7) || ' days')"
        cm_bucket = f"strftime('%Y-%m-%d', datetime(cm.created_at, '{tz_shift}'), '-' || ((strftime('%w', datetime(cm.created_at, '{tz_shift}')) + 6) % 7) || ' days')"
    else:
        ss_bucket = f"strftime('%Y-%m-%d', datetime(ss_inner.created_at, '{tz_shift}'))"
        cm_bucket = f"strftime('%Y-%m-%d', datetime(cm.created_at, '{tz_shift}'))"
    if not days:
        limit = 5000
    elif period == "hourly":
        limit = days * 24 + 1
    elif period == "weekly":
        limit = max(days // 7, 1) + 1
    else:
        limit = days + 1
    cutoff = _stats_cutoff(days)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)

    ss_clauses: list[str] = ["ss_inner.created_at IS NOT NULL"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("datetime(ss_inner.created_at) >= datetime(?)")
        ss_params.append(cutoff)
    _append_stats_in_filter(ss_clauses, ss_params, _stats_agent_expr("ss_inner."), agents)
    _append_stats_in_filter(ss_clauses, ss_params, "ss_inner.topic", topics)

    cm_clauses: list[str] = ["cm.role = 'assistant'"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(cm.created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.agent", agents)
    _append_stats_in_filter(cm_clauses, cm_params, "cm.topic", topics)

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
                        ss.duration_ms,
                        COALESCE(cm.session_turns, 0) AS session_turns,
                        COALESCE(cm.adhoc_turns, 0) AS adhoc_turns,
                        COALESCE(cm.session_turns, 0) + COALESCE(cm.adhoc_turns, 0) AS total_turns,
                        cm.message_ids
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
                                     THEN quota_after - quota_before ELSE NULL END) AS quota_delta,
                            AVG(duration_ms) AS duration_ms
                        FROM session_stats ss_inner
                        WHERE {ss_where}
                        GROUP BY period ORDER BY period DESC LIMIT ?
                    ) ss
                    LEFT JOIN (
                        SELECT {cm_bucket} AS period,
                               {s_expr} AS session_turns,
                               {a_expr} AS adhoc_turns,
                               GROUP_CONCAT(cm.id) AS message_ids
                        FROM chat_messages cm
                        WHERE {cm_where}
                        GROUP BY period
                    ) cm ON ss.period = cm.period
                    ORDER BY ss.period DESC""",
                (*ss_params, limit, *cm_params),
            ).fetchall()
            result = [dict(r) for r in rows]
            _merge_chart_aggregates(
                conn,
                result,
                period=period,
                days=days,
                agent=agent,
                topic=topic,
                adhoc=adhoc,
                tz_offset_minutes=tz_offset_minutes,
                chart_series=chart_series,
            )
        return result
    except sqlite3.OperationalError:
        return []


def get_stats_by_turn(
    days: int = 30, hours: int = 0, agent: str = "", topic: str = "", adhoc: str = "all",
    limit: int = 2000,
) -> list:
    # One row per completed turn, sourced from run_events rather than session_stats:
    # session_stats is keyed by session_id and gets overwritten on every resumed
    # turn of a multi-turn conversation, so it only ever holds the *latest* turn's
    # numbers. The per-turn "stats" event recorded in run_events at completion time
    # is the only place a given turn's own duration/cost/tokens survive later turns.
    cutoff = _stats_cutoff(days, hours)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)

    clauses: list[str] = ["cm.role = 'assistant'"]
    params: list = []
    if cutoff:
        clauses.append("datetime(cm.created_at) >= datetime(?)")
        params.append(cutoff)
    _append_stats_in_filter(clauses, params, "cm.agent", agents)
    _append_stats_in_filter(clauses, params, "cm.topic", topics)
    if adhoc == "session":
        clauses.append("COALESCE(cm.adhoc, 0) = 0")
    elif adhoc == "adhoc":
        clauses.append("COALESCE(cm.adhoc, 0) = 1")
    where = " AND ".join(clauses)

    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""SELECT cm.id AS msg_id, cm.created_at AS period, cm.topic,
                           cm.agent, cm.adhoc, cm.quota_delta, re.payload
                    FROM chat_messages cm
                    JOIN run_events re
                        ON re.id = (
                            SELECT MAX(re2.id) FROM run_events re2
                            WHERE re2.msg_id = cm.id AND re2.event_type = 'stats'
                        )
                    WHERE {where}
                    ORDER BY cm.id DESC
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
    tz_offset_minutes: int,
    chart_series: Optional[list[dict]],
    breakdown: str,
) -> None:
    if not rows or not chart_series:
        return
    tz_shift = f"{-tz_offset_minutes} minutes"
    if period == "hourly":
        bucket = f"strftime('%Y-%m-%d %H:00', datetime(created_at, '{tz_shift}'))"
    elif period == "weekly":
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'), '-' || ((strftime('%w', datetime(created_at, '{tz_shift}')) + 6) % 7) || ' days')"
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

    cutoff = _stats_cutoff(days)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)
    clauses: list[str] = ["created_at IS NOT NULL"]
    params: list = []
    if cutoff:
        clauses.append("datetime(created_at) >= datetime(?)")
        params.append(cutoff)
    _append_stats_in_filter(clauses, params, _stats_agent_expr(), agents)
    _append_stats_in_filter(clauses, params, "topic", topics)
    if adhoc == "session":
        clauses.append("COALESCE(adhoc, 0) = 0")
    elif adhoc == "adhoc":
        clauses.append("COALESCE(adhoc, 0) = 1")
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
    tz_offset_minutes: int = 0,
    include_session: bool = False,
    chart_series: Optional[list[dict]] = None,
) -> list:
    breakdown = "agent_session" if include_session else "agent"
    return get_stats_by_breakdown(period, days, agent, topic, adhoc, tz_offset_minutes, breakdown, chart_series)


def get_stats_by_breakdown(
    period: str = "daily",
    days: int = 30,
    agent: str = "",
    topic: str = "",
    adhoc: str = "all",
    tz_offset_minutes: int = 0,
    breakdown: str = "agent",
    chart_series: Optional[list[dict]] = None,
) -> list:
    tz_shift = f"{-tz_offset_minutes} minutes"
    if period == "hourly":
        bucket = f"strftime('%Y-%m-%d %H:00', datetime(created_at, '{tz_shift}'))"
    elif period == "weekly":
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'), '-' || ((strftime('%w', datetime(created_at, '{tz_shift}')) + 6) % 7) || ' days')"
    else:
        bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'))"
    include_topic = breakdown in {"topic_agent", "topic_agent_session"}
    include_session = breakdown in {"agent_session", "topic_agent_session"}
    base_agent = _stats_agent_expr()
    cm_base_agent = "COALESCE(agent, 'unknown')"
    agent_key_expr = base_agent
    cm_agent_key_expr = cm_base_agent
    if include_session:
        agent_key_expr = f"{base_agent} || CASE WHEN COALESCE(adhoc, 0) = 1 THEN '!' ELSE '' END"
        cm_agent_key_expr = f"{cm_base_agent} || CASE WHEN COALESCE(adhoc, 0) = 1 THEN '!' ELSE '' END"
    if period == "hourly":
        cm_bucket = f"strftime('%Y-%m-%d %H:00', datetime(created_at, '{tz_shift}'))"
    elif period == "weekly":
        cm_bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'), '-' || ((strftime('%w', datetime(created_at, '{tz_shift}')) + 6) % 7) || ' days')"
    else:
        cm_bucket = f"strftime('%Y-%m-%d', datetime(created_at, '{tz_shift}'))"
    if not days:
        limit = 5000
    elif period == "hourly":
        limit = days * 24 + 1
    elif period == "weekly":
        limit = max(days // 7, 1) + 1
    else:
        limit = days + 1
    cutoff = _stats_cutoff(days)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)

    ss_clauses: list[str] = ["created_at IS NOT NULL"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("datetime(created_at) >= datetime(?)")
        ss_params.append(cutoff)
    _append_stats_in_filter(ss_clauses, ss_params, _stats_agent_expr(), agents)
    _append_stats_in_filter(ss_clauses, ss_params, "topic", topics)

    cm_clauses: list[str] = ["role = 'assistant'", "created_at IS NOT NULL"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_in_filter(cm_clauses, cm_params, "agent", agents)
    _append_stats_in_filter(cm_clauses, cm_params, "topic", topics)

    if adhoc == "session":
        ss_clauses.append("COALESCE(adhoc, 0) = 0")
        cm_clauses.append("COALESCE(adhoc, 0) = 0")
    elif adhoc == "adhoc":
        ss_clauses.append("COALESCE(adhoc, 0) = 1")
        cm_clauses.append("COALESCE(adhoc, 0) = 1")

    ss_where = " AND ".join(ss_clauses)
    cm_where = " AND ".join(cm_clauses)
    ss_dims = ["period", f"{agent_key_expr} AS agent_key", f"{agent_key_expr} AS agent"]
    cm_dims = ["period", f"{cm_agent_key_expr} AS agent_key", f"{cm_agent_key_expr} AS agent"]
    select_dims = ["ss.period", "ss.agent_key", "ss.agent"]
    union_dims = ["cm.period", "cm.agent_key", "cm.agent"]
    join_dims = ["cm.period = ss.period", "cm.agent_key = ss.agent_key"]
    group_dims = ["period", "agent_key"]
    if include_topic:
        ss_dims.insert(1, "topic")
        cm_dims.insert(1, "topic")
        select_dims.insert(1, "ss.topic")
        union_dims.insert(1, "cm.topic")
        join_dims.append("cm.topic = ss.topic")
        group_dims.append("topic")
    if include_session:
        session_type = "CASE WHEN COALESCE(adhoc, 0) = 1 THEN 'adhoc' ELSE 'session' END"
        cm_session_type = "CASE WHEN COALESCE(adhoc, 0) = 1 THEN 'adhoc' ELSE 'session' END"
        ss_dims.append(f"{session_type} AS session_type")
        cm_dims.append(f"{cm_session_type} AS session_type")
        select_dims.append("ss.session_type")
        union_dims.append("cm.session_type")
        join_dims.append("cm.session_type = ss.session_type")
        group_dims.append("session_type")

    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""WITH ss AS (
                        SELECT
                            {bucket} AS period,
                            {', '.join(ss_dims[1:])},
                            COUNT(*) AS sessions,
                            SUM(input_tokens) AS input_tokens,
                            SUM(input_tokens - COALESCE(history_input_tokens, 0)) AS new_input_tokens,
                            SUM(output_tokens) AS output_tokens,
                            SUM(cache_read_tokens) AS cache_read_tokens,
                            SUM(cache_write_tokens) AS cache_write_tokens,
                            SUM(cost_usd) AS cost_usd,
                            SUM(CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL
                                     THEN quota_after - quota_before ELSE NULL END) AS quota_delta,
                            AVG(duration_ms) AS duration_ms
                        FROM session_stats
                        WHERE {ss_where}
                        GROUP BY {', '.join(group_dims)}
                    ), cm AS (
                        SELECT
                            {cm_bucket} AS period,
                            {', '.join(cm_dims[1:])},
                            COUNT(*) AS total_turns,
                            GROUP_CONCAT(id) AS message_ids
                        FROM chat_messages
                        WHERE {cm_where}
                        GROUP BY {', '.join(group_dims)}
                    )
                    SELECT *
                    FROM (
                        SELECT
                            {', '.join(select_dims)},
                            ss.sessions,
                            COALESCE(cm.total_turns, ss.sessions, 0) AS total_turns,
                            cm.message_ids,
                            ss.input_tokens,
                            ss.new_input_tokens,
                            ss.output_tokens,
                            ss.cache_read_tokens,
                            ss.cache_write_tokens,
                            ss.cost_usd,
                            ss.quota_delta,
                            ss.duration_ms
                        FROM ss
                        LEFT JOIN cm ON {' AND '.join(join_dims)}
                        UNION ALL
                        SELECT
                            {', '.join(union_dims)},
                            0 AS sessions,
                            cm.total_turns,
                            cm.message_ids,
                            0 AS input_tokens,
                            0 AS new_input_tokens,
                            0 AS output_tokens,
                            0 AS cache_read_tokens,
                            0 AS cache_write_tokens,
                            0 AS cost_usd,
                            NULL AS quota_delta,
                            NULL AS duration_ms
                        FROM cm
                        LEFT JOIN ss ON {' AND '.join(join_dims)}
                        WHERE ss.period IS NULL
                    )
                    ORDER BY period DESC, sessions DESC
                    LIMIT ?""",
                (*ss_params, *cm_params, limit * 20),
            ).fetchall()
            result = [dict(r) for r in rows]
            _merge_breakdown_chart_aggregates(
                conn,
                result,
                period=period,
                days=days,
                agent=agent,
                topic=topic,
                adhoc=adhoc,
                tz_offset_minutes=tz_offset_minutes,
                chart_series=chart_series,
                breakdown=breakdown,
            )
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
    days: int = 30, agent: str = "", topic: str = "", adhoc: str = "all"
) -> list:
    cutoff = _stats_cutoff(days)
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)

    ss_clauses: list[str] = ["1=1"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("datetime(created_at) >= datetime(?)")
        ss_params.append(cutoff)
    _append_stats_in_filter(ss_clauses, ss_params, "topic", topics)
    _append_stats_in_filter(ss_clauses, ss_params, _stats_agent_expr(), agents)

    cm_clauses: list[str] = ["role = 'assistant'"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_in_filter(cm_clauses, cm_params, "topic", topics)
    _append_stats_in_filter(cm_clauses, cm_params, "agent", agents)

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
                          ss.duration_ms,
                          COALESCE(cm.turns, 0) AS total_turns
                   FROM (
                       SELECT {_stats_agent_expr()} AS agent,
                              COUNT(*) AS sessions,
                              SUM(input_tokens) AS input_tokens,
                              SUM(output_tokens) AS output_tokens,
                              SUM(cache_read_tokens) AS cache_read_tokens,
                              SUM(cache_write_tokens) AS cache_write_tokens,
                              SUM(cost_usd) AS cost_usd,
                              SUM(CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL
                                       THEN quota_after - quota_before ELSE NULL END) AS quota_delta,
                              AVG(duration_ms) AS duration_ms
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
    agents = _stats_filter_values(agent)
    topics = _stats_filter_values(topic)

    ss_clauses: list[str] = ["topic IS NOT NULL"]
    ss_params: list = []
    if cutoff:
        ss_clauses.append("datetime(created_at) >= datetime(?)")
        ss_params.append(cutoff)
    _append_stats_in_filter(ss_clauses, ss_params, _stats_agent_expr(), agents)
    _append_stats_in_filter(ss_clauses, ss_params, "topic", topics)

    cm_clauses: list[str] = ["role = 'assistant'"]
    cm_params: list = []
    if cutoff:
        cm_clauses.append("datetime(created_at) >= datetime(?)")
        cm_params.append(cutoff)
    _append_stats_in_filter(cm_clauses, cm_params, "agent", agents)
    _append_stats_in_filter(cm_clauses, cm_params, "topic", topics)

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
                          ss.duration_ms,
                          COALESCE(cm.turns, 0) AS total_turns
                   FROM (
                       SELECT topic,
                              COUNT(*) AS sessions,
                              SUM(input_tokens) AS input_tokens,
                              SUM(output_tokens) AS output_tokens,
                              SUM(cache_read_tokens) AS cache_read_tokens,
                              SUM(cost_usd) AS cost_usd,
                              SUM(CASE WHEN quota_before IS NOT NULL AND quota_after IS NOT NULL
                                       THEN quota_after - quota_before ELSE NULL END) AS quota_delta,
                              AVG(duration_ms) AS duration_ms
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
                    f"SELECT DISTINCT {_stats_agent_expr()} FROM session_stats"
                    f" WHERE agent IS NOT NULL OR harness IS NOT NULL ORDER BY 1"
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


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------

def save_worktree(topic: str, agent: str, repo_root: str, wt_path: str, br: str) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO worktrees (topic, agent, repo_root, worktree_path, branch_name)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(topic, agent, repo_root) DO UPDATE SET
                 last_used_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), status='active'""",
            (topic, agent, repo_root, wt_path, br),
        )


def mark_worktree_synced(topic: str, agent: str, repo_root: str) -> None:
    """Record that a turn's changes were merged, without deleting the worktree
    yet — actual removal is a later best-effort sweep (see worktree.cleanup_worktrees),
    so a background process the turn spawned isn't left with its cwd yanked out."""
    with _connect() as conn:
        conn.execute(
            """UPDATE worktrees SET status='synced',
                 last_used_at=strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE topic=? AND agent=? AND repo_root=?""",
            (topic, agent, repo_root),
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
