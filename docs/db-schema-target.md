# Squid — Target DB Schema

This is the intended clean schema for the pre-launch DB collapse.
When ready: delete `_MIGRATIONS`, replace `_TABLES` with this, rewrite `init_db()` to
create-only + seed defaults. No migration logic needed.

The seeding logic (`INSERT OR IGNORE` per installed CLI) is already implemented in `init_db()`
and can be kept as-is. The `_MIGRATIONS` list and the table-recreation block for the old
topics schema are the only things to remove.

---

## Tables

### `agents`
```sql
CREATE TABLE agents (
    name       TEXT PRIMARY KEY,
    backend    TEXT NOT NULL,             -- claude | codex | cursor | antigravity | copilot
    model      TEXT,                      -- null = backend default
    cwd        TEXT,                      -- null = /tmp/squid
    timeout    INTEGER,                   -- null = global default (1800s)
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

### `topics`
```sql
CREATE TABLE topics (
    topic        TEXT NOT NULL,
    agent        TEXT NOT NULL DEFAULT '', -- '' = topic-level row; agent name = agent-level row
    sticky_agent TEXT,                     -- topic-level only: last used agent
    last_prompt  TEXT,                     -- last user prompt sent to this topic/agent
    last_at      TEXT,                     -- timestamp of last_prompt
    last_model   TEXT,                     -- model from agent config at dispatch time
    last_backend TEXT,                     -- backend from agent config at dispatch time
    hidden       INTEGER DEFAULT 0,        -- 1 = soft-deleted (excluded from autocomplete)
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (topic, agent)
);
```

Two row types per topic:
- `(topic, '')` — topic-level: holds `sticky_agent`, `hidden`, `last_prompt`, `last_model`, `last_backend` across all agents
- `(topic, 'agentname')` — agent-level: holds `last_prompt`, `last_model`, `last_backend` for that specific agent (drives `#topic@agent` autocomplete)

### `chat_messages`
```sql
CREATE TABLE chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL DEFAULT 'default',
    agent      TEXT,                      -- agent name at time of message
    session_id TEXT,                      -- set after _stats arrive
    role       TEXT NOT NULL,             -- user | assistant
    content    TEXT,                      -- null while pending
    reply_to   INTEGER REFERENCES chat_messages(id),
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
    adhoc      INTEGER DEFAULT 0,         -- 1 = adhoc turn
    context    TEXT,                      -- user: JSON [msg_id, …]; assistant: JSON tool_use events
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

### `topic_sessions`
```sql
CREATE TABLE topic_sessions (
    topic       TEXT NOT NULL,
    agent       TEXT NOT NULL,
    session_id  TEXT NOT NULL,            -- passed to CLI as --resume
    cwd         TEXT NOT NULL,            -- locked at session creation
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
    quota_before         REAL,    -- backend quota percentage at turn start, when exposed
    quota_after          REAL,    -- backend quota percentage at turn end, when exposed
    lookback             INTEGER DEFAULT 0,
    created_at           TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

---

## Seed Defaults (on first run)

Detect installed CLIs and create one agent per available backend:

```python
from .config import CLAUDE_PATH, CODEX_PATH, COPILOT_PATH, CURSOR_PATH, AGY_PATH

DEFAULTS = [
    ("claude",       "claude",       CLAUDE_PATH),
    ("codex",        "codex",        CODEX_PATH),
    ("cursor",       "cursor",       CURSOR_PATH),
    ("antigravity",  "antigravity",  AGY_PATH),
    ("copilot",      "copilot",      COPILOT_PATH),
]

for name, backend, path in DEFAULTS:
    if path:
        conn.execute(
            "INSERT OR IGNORE INTO agents (name, backend) VALUES (?, ?)",
            (name, backend)
        )
```

---

## Open Questions / Pending Decisions

- Should `session_stats.model` be backfilled from the CLI's actual reported model?
  Currently it comes from agent config at dispatch time (accurate for explicit configs,
  null when model is not set and CLI auto-selects).
- Index candidates: `chat_messages(topic, role, status)`, `session_stats(topic, created_at)`
