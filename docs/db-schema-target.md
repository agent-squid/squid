# Squid — DB Schema

This is the schema used by `agent/stats_db.py`. `init_db()` creates all
tables/indexes with `CREATE ... IF NOT EXISTS` and seeds default agents for
installed harnesses. Squid has no versioned migration framework; instead,
`init_db()` runs a few idempotent `ALTER TABLE ... ADD COLUMN` guarded by
`PRAGMA table_info` checks on every boot to backfill newer columns onto
existing databases (see "Column backfills" below).

---

## Tables

### `agents`
```sql
CREATE TABLE agents (
    name       TEXT PRIMARY KEY,
    harness    TEXT NOT NULL,             -- coded CLI integration, e.g. claudecode/codex
    provider   TEXT,                      -- provider id; null = harness default provider
    model      TEXT,                      -- null = provider/harness default
    cwd        TEXT,                      -- null = /tmp/<user>/squid
    timeout    INTEGER,                   -- null = global default (1800s)
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

On databases created before the harness/provider split, a legacy `backend`
column (e.g. `"claudecode:anthropic"`) may still exist; `init_db()` detects it
via `PRAGMA table_info` and keeps it populated on insert/upsert for backward
compat, but it is not part of the current DDL.

### `topics`
```sql
CREATE TABLE topics (
    topic        TEXT NOT NULL,
    agent        TEXT NOT NULL DEFAULT '', -- '' = topic-level row; agent name = agent-level row
    sticky_agent TEXT,                     -- topic-level only: last used agent
    sticky_adhoc INTEGER DEFAULT 0,        -- topic-level only: last selected mode
    last_prompt  TEXT,                     -- last user prompt sent to this topic/agent
    last_adhoc_prompt TEXT,                -- last adhoc prompt sent to this topic/agent
    last_at      TEXT,                     -- timestamp of last_prompt
    last_session_at TEXT,                  -- timestamp of last non-adhoc (session) prompt
    last_adhoc_at   TEXT,                  -- timestamp of last adhoc prompt
    last_model   TEXT,                     -- model from agent config at dispatch time
    last_harness TEXT,                     -- harness from agent config at dispatch time
    last_provider TEXT,                    -- provider from agent config at dispatch time
    hidden       INTEGER DEFAULT 0,        -- 1 = soft-deleted (excluded from autocomplete)
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (topic, agent)
);
```

Two row types per topic:
- `(topic, '')` — topic-level: holds `sticky_agent`, `sticky_adhoc`, `hidden`, and last-run metadata across all agents
- `(topic, 'agentname')` — agent-level: holds `last_prompt`, `last_adhoc_prompt`, `last_session_at`, `last_adhoc_at`, `last_model`, `last_harness`, and `last_provider` for that specific agent (drives `#topic@agent` autocomplete)

### `chat_messages`
```sql
CREATE TABLE chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL DEFAULT 'default',
    agent       TEXT,                      -- agent name at time of message
    session_id  TEXT,                      -- known resume id at dispatch; otherwise set after _stats arrive
    role        TEXT NOT NULL,             -- user | assistant
    content     TEXT,                      -- null while pending
    reply_to    INTEGER REFERENCES chat_messages(id),
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error | cancelled
    adhoc       INTEGER DEFAULT 0,         -- 1 = adhoc turn
    context     TEXT,                      -- user: JSON [msg_id, …]; assistant: JSON tool_use events
    status_raw  TEXT,                      -- raw harness status/debug text
    flow_run_id TEXT,                      -- short id for one Squid Flow route execution
    flow_route  TEXT,                      -- canonical Squid Flow route expression
    session_turn_index INTEGER,            -- ordinal assistant turn within a non-adhoc session
    lookback    INTEGER DEFAULT 0,
    quota_delta  REAL,
    quota_before REAL,
    quota_after  REAL,
    completed_at TEXT,                     -- timestamp status became done|error|cancelled
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

`cancelled` and `completed_at` capture terminal outcomes for killed/dequeued
turns so they're visible in Stats without a restart. See
[ADR-0033](decisions/0033-cancelled-and-error-turn-capture.md).

Squid Flow run ids are allocated from `id_counters` namespace `flow_run` and
stored as text (`"1"`, `"2"`, `"3"`, ...). Existing UUID-style values are valid
legacy data.

### `id_counters`
```sql
CREATE TABLE id_counters (
    namespace TEXT PRIMARY KEY,
    next_id   INTEGER NOT NULL
);
```

Generic allocator for local human-readable ids. `next_id` is the next value to
return for that namespace.

### `topic_sessions`
```sql
CREATE TABLE topic_sessions (
    topic       TEXT NOT NULL,
    agent       TEXT NOT NULL,
    session_id  TEXT NOT NULL,            -- passed to CLI as --resume
    cwd         TEXT NOT NULL,            -- locked at session creation
    runtime_fingerprint TEXT,             -- harness/provider/protocol execution fingerprint
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (topic, agent)
);
```

### `session_stats`
```sql
CREATE TABLE session_stats (
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
    quota_before         REAL,    -- observed provider-wide quota percentage at turn start
    quota_after          REAL,    -- observed provider-wide quota percentage after turn completion
    adhoc                INTEGER DEFAULT 0,
    lookback             INTEGER DEFAULT 0,
    created_at           TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

API responses may still expose a computed `backend` compatibility value derived
from `harness` and `provider`; SQLite does not store it.

### `run_events`
```sql
CREATE TABLE run_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id     INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(msg_id, seq)
);
```

### `git_diff_reverts`
```sql
CREATE TABLE git_diff_reverts (
    msg_id      INTEGER NOT NULL,
    repo        TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    reverted_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (msg_id, repo, file_path)
);
```

### `file_edit_history`
```sql
CREATE TABLE file_edit_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL,
    before      TEXT NOT NULL,
    after       TEXT NOT NULL,
    edited_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_file_edit_history_path ON file_edit_history (file_path, id DESC);
```

### `bookmarks`
```sql
CREATE TABLE bookmarks (
    msg_id      INTEGER PRIMARY KEY,
    topic       TEXT,
    agent       TEXT,
    content     TEXT,
    saved_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

### `worktrees`
```sql
CREATE TABLE worktrees (
    topic           TEXT NOT NULL,
    agent           TEXT NOT NULL,
    repo_root       TEXT NOT NULL,
    worktree_path   TEXT NOT NULL,
    branch_name     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_used_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (topic, agent, repo_root)
);
```

### `stats_filter_presets`
```sql
CREATE TABLE stats_filter_presets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    state_json    TEXT NOT NULL,
    is_default    INTEGER NOT NULL DEFAULT 0,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE UNIQUE INDEX idx_stats_filter_presets_one_default
ON stats_filter_presets(is_default)
WHERE is_default = 1;
```

### `messages_fts` / `prompts_fts` — full-text search
Standalone FTS5 tables (not external-content), kept in sync by triggers. See ADR-0021.

```sql
CREATE VIRTUAL TABLE messages_fts USING fts5(content, tokenize='unicode61');

CREATE TRIGGER messages_fts_sync
AFTER UPDATE OF content ON chat_messages
WHEN NEW.role = 'assistant' AND NEW.content IS NOT NULL AND NEW.status = 'done'
BEGIN
    DELETE FROM messages_fts WHERE rowid = NEW.id;
    INSERT INTO messages_fts(rowid, content) VALUES (NEW.id, NEW.content);
END;

CREATE VIRTUAL TABLE prompts_fts USING fts5(content, tokenize='unicode61');

CREATE TRIGGER prompts_fts_sync
AFTER INSERT ON chat_messages
WHEN NEW.role = 'user' AND NEW.content IS NOT NULL
BEGIN
    INSERT INTO prompts_fts(rowid, content) VALUES (NEW.id, NEW.content);
END;
```

The `messages_fts_sync` trigger fires on the `status='done'` update (final
content), not the first partial-content save, so the index always holds the
complete response. `init_db()` also repairs `messages_fts` on every boot:
it deletes rows whose indexed content no longer matches the final
`chat_messages.content`, and backfills any `done` assistant messages missing
from the index.

---

## Column backfills (run every boot, guarded by `PRAGMA table_info`)

`init_db()` adds these columns via `ALTER TABLE ... ADD COLUMN` if a database
predates them — additive-only, no down migration:
- `topics.last_session_at TEXT`
- `topics.last_adhoc_at TEXT`
- `chat_messages.completed_at TEXT`
- `chat_messages.flow_run_id TEXT`
- `chat_messages.flow_route TEXT`

---

## Seed Defaults (on first run)

Detect installed CLIs and create one agent per available harness:

```python
from .harnesses import SUPPORTED_HARNESSES, is_installed
from .resolve import agent_ref_for_storage

for harness in sorted(SUPPORTED_HARNESSES):
    if harness != "claudecode" and is_installed(harness):
        provider = {"pi": "nvidia"}.get(harness)
        model = {"pi": "deepseek-ai/deepseek-v4-pro"}.get(harness)
        conn.execute(
            "INSERT OR IGNORE INTO agents (name, harness, provider, model) VALUES (?, ?, ?, ?)",
            (harness, harness, provider, model)
        )

if is_installed("claudecode"):
    conn.execute(
        "INSERT OR IGNORE INTO agents (name, harness, provider, model) VALUES (?, ?, ?, ?)",
        ("claude", "claudecode", "anthropic", None)
    )
```

---

## Open Questions / Pending Decisions

- Should `session_stats.model` be backfilled from the CLI's actual reported model?
  Currently it comes from agent config at dispatch time (accurate for explicit configs,
  null when model is not set and CLI auto-selects).
- Index candidates: `chat_messages(topic, role, status)`, `session_stats(topic, created_at)`
