# Squid — API & Data Model

## Terminology

| Term | Meaning |
|---|---|
| **alias** | The short user-defined name for an agent config (e.g. `clawd`, `code`). Stored as `alias` in the DB. Called `agent` in the HTTP API and UI layer. |
| **agent** | An alias + its configuration: backend, model, cwd, timeout. One row in the `aliases` table. |
| **topic** | A named conversation channel (e.g. `work`, `personal`). Each topic has a sticky alias and zero or more active sessions. |
| **session** | A resumable CLI process context identified by a `session_id` (from `claude --resume`) or `thread_id` (Codex). Scoped to `(topic, alias)`. |
| **adhoc** | A one-off parallel turn. Uses `lookback` exchanges as context instead of a persistent session. |

---

## HTTP API

Base URL: `http://localhost:8000`

---

### POST /chat

Stream an AI response. Returns `text/event-stream`.

**Request body**
```json
{
  "message":  "string (required)",
  "topic":    "string (default: \"default\")",
  "agent":    "string | null  — alias name; null = use topic sticky",
  "lookback": "integer (default: 0) — adhoc context window",
  "adhoc":    "boolean (default: false)"
}
```

**SSE event stream** (in order)

| Event | Data | When |
|---|---|---|
| `meta` | `{"agent": str\|null, "backend": str, "msg_id": int, "adhoc": bool}` | First, always |
| `queued` | `{"topic": str, "position": int}` | When behind another turn in the topic queue |
| `data:` (chunk) | raw text fragment | During streaming |
| `status` | status line text (one line) | When CLI emits non-content output |
| `tool` | `{"type": str, "name": str, ...}` | When backend calls a tool |
| `stats` | see Stats object below | On session completion |
| `done` | _(empty)_ | Normal completion |
| `error` | error message string | On CLI error with no prior content |

**Stats object** (sent in the `stats` event and stored in `session_stats`)
```json
{
  "session_id":            "string",
  "input_tokens":          0,
  "output_tokens":         0,
  "cache_read_tokens":     0,
  "cache_write_tokens":    0,
  "history_input_tokens":  0,
  "cost_usd":              0.0,
  "duration_ms":           0,
  "adhoc":                 false,
  "lookback":              0,
  "pin_count":             0,
  "cwd":                   "/path/to/cwd"
}
```

**Error responses**
```json
{ "error": "Agent 'name' not found. Create it first via /config/agents." }  // 400
```

---

### POST /cmd

Run a topic-scoped control command.

**Request body**
```json
{
  "command": "stop | stopall | deq | list | restart | clear | compact",
  "topic":   "string (default: \"default\")",
  "agent":   "string | null  — required for clear/compact if no sticky",
  "pos":     "integer | null  — deq only: null=all, 1=first, -1=last"
}
```

**Response**
```json
{ "ok": true }
{ "ok": true, "killed": true }           // stop
{ "ok": true, "killed": int, "drained": int }  // stopall
{ "ok": true, "drained": int }           // deq
{ "ok": true, "agent": "alias-name" }    // clear / compact
{ "ok": true, "topics": [...] }          // list
{ "ok": false, "error": "..." }          // 400
```

---

### GET /history

**Query params**: `topic`, `alias`, `offset` (default 0), `limit` (default 5)

**Response**
```json
{
  "items": [
    {
      "id":         1,
      "role":       "user | assistant",
      "topic":      "string",
      "alias":      "string | null",
      "backend":    "string | null",
      "model":      "string | null",
      "session_id": "string | null",
      "content":    "string | null",
      "status":     "done | pending | error",
      "pinned":     0,
      "adhoc":      0,
      "tools":      "json string | null",
      "timestamp":  "ISO8601",
      "reply_to":   1,
      "prompt":     "user message text (for assistant rows)",
      "stats": {
        "session_id": "...",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "...": "all session_stats columns"
      }
    }
  ],
  "total":    100,
  "has_more": true
}
```

---

### GET /topics

**Response** — array of topic summaries, ordered by most recent activity:
```json
[
  {
    "name":         "string",
    "alias":        "string | null  — sticky agent",
    "last_model":   "string | null",
    "last_backend": "string | null",
    "last_prompt":  "string | null",
    "last_at":      "ISO8601 | null",
    "queue_depth":  0,
    "active":       false
  }
]
```

---

### DELETE /topics/{topic}

Removes topic and all active session state. Chat history preserved.

**Response**: `{ "ok": true | false }`

---

### GET /topics/{topic}/sessions

Returns all aliases with active sessions for a topic.

**Response**: `{ "aliases": [{ "alias": str, "session_id": str, "cwd": str, "created_at": str }] }`

---

### GET /topics/{topic}/session?alias={alias}

Returns the active session state for a `(topic, alias)` pair.

**Response**
```json
{
  "session_id":        "string | null",
  "cwd":               "string | null",
  "pending_injections": [{ "id": int, "role": str, "content": str, "source_alias": str, "adhoc": int }],
  "already_injected":   [{ "msg_id": int, "injected_at": str }]
}
```

---

### DELETE /topics/{topic}/session?alias={alias}

Clears the active session for `(topic, alias)`. Next message starts fresh.

**Response**: `{ "ok": true }`

---

### GET /context/{topic}?alias={alias}

Alias for `GET /topics/{topic}/session`. Returns session state + injection log.

---

### GET /chat/{msg_id}/status

Poll a single message for status (used when client reconnects mid-stream).

**Response**: `{ "id": int, "status": "pending | done | error", "content": str | null }`

---

### POST /chat/{msg_id}/pin

Set pin state for a message.

**Request body**: `{ "pinned": 1 | 0 | -1 }`  
`1` = always inject into future sessions, `0` = default, `-1` = always exclude.

**Response**: `{ "ok": true }`

---

### POST /chat/reset-pins

Clear all pins (set `pinned=0` everywhere).

**Response**: `{ "ok": true }`

---

### GET /config/agents

**Response** — array of agent configs:
```json
[
  {
    "name":       "string",
    "backend":    "auto | claude | cursor | antigravity | codex | copilot",
    "model":      "string | null",
    "cwd":        "string | null",
    "timeout":    300,
    "created_at": "ISO8601"
  }
]
```

---

### POST /config/agents

Create or update an agent (upsert by name).

**Request body**
```json
{
  "name":    "string (required)",
  "backend": "auto | claude | cursor | antigravity | codex | copilot",
  "model":   "string | null",
  "cwd":     "string | null  — abs path; null = /tmp/squid",
  "timeout": "integer | null  — seconds"
}
```

**Response**: `{ "ok": true }`

---

### DELETE /config/agents/{name}

Delete an agent and its topic sessions.

**Response**: `{ "ok": true | false }`

---

### GET /stats

**Query params**: `period` (`daily` | `hourly`, default `daily`), `group` (`time` | `topic` | `agent`, default `time`)

**Response — group=time (daily/hourly)**
```json
[
  {
    "period":            "2026-05-28",
    "sessions":          12,
    "input_tokens":      48000,
    "new_input_tokens":  10000,
    "output_tokens":     6000,
    "cache_read_tokens": 30000,
    "cache_write_tokens": 8000,
    "cost_usd":          0.42,
    "quota_delta":       -0.05
  }
]
```

**Response — group=topic**
```json
[{ "topic": "work", "sessions": 5, "input_tokens": 20000, "output_tokens": 3000, "cost_usd": 0.18 }]
```

**Response — group=agent**
```json
[{ "agent": "clawd", "sessions": 8, "input_tokens": 32000, "output_tokens": 5000, "cache_read_tokens": 20000, "cache_write_tokens": 5000, "cost_usd": 0.30 }]
```

Note: `agent` is `COALESCE(alias, backend, 'unknown')`. See [open issue](#open-issues) for per-(alias, backend, model) breakdown.

---

### POST /stats/quota-delta

Record the before/after claude.ai quota usage for a session.

**Request body**: `{ "session_id": "string", "before": 0.0, "after": 0.0 }`

**Response**: `{ "ok": true }`

---

### POST /config/creds

Save claude.ai session credentials (org ID + session key).

**Request body**: `{ "org_id": "string", "session_key": "string" }`

**Response**: `{ "ok": true }`

---

### GET /quota

Fetch current claude.ai usage from `claude.ai/api/organizations/{org_id}/usage`. Requires saved credentials.

**Response**: proxied JSON from claude.ai, or `{ "error": "..." }` (400/502).

---

### GET /processes

**Response** — array of active CLI processes:
```json
[{ "topic": "work", "alias": "clawd", "pid": 1234, "backend": "claude", "started_at": "ISO8601" }]
```

---

### GET /health

**Response**
```json
{
  "status":    "ok",
  "boot_time": "ISO8601",
  "backends": {
    "claude":      { "available": true,  "path": "/usr/local/bin/claude" },
    "cursor":      { "available": false, "path": null },
    "antigravity": { "available": false, "path": null },
    "codex":       { "available": true,  "path": "/usr/local/bin/codex" },
    "copilot":     { "available": false, "path": null }
  }
}
```

---

## Data Model

SQLite database at `squid.db` (project root).

---

### `aliases` — agent configurations

```
name       TEXT  PK          user-defined short name
backend    TEXT  NOT NULL    auto | claude | cursor | antigravity | codex | copilot
model      TEXT              model string (e.g. claude-opus-4-5); null = backend default
cwd        TEXT              working directory; null = /tmp/squid
timeout    INTEGER           per-agent response timeout in seconds; null = global default
created_at TEXT              ISO8601
```

---

### `topics` — topic registry

```
name       TEXT  PK          topic name
alias      TEXT              sticky agent (last-used alias for this topic)
created_at TEXT              ISO8601
```

---

### `chat_messages` — full message history

```
id         INTEGER  PK  AUTOINCREMENT
topic      TEXT     NOT NULL   default: "default"
alias      TEXT                agent alias at time of message
backend    TEXT                backend used
model      TEXT                model used (from _stats or agent config)
session_id TEXT                CLI session_id (set after stats arrive)
role       TEXT     NOT NULL   user | assistant
content    TEXT                message body (may be null while pending)
reply_to   INTEGER  FK → id    assistant rows point to their user message
status     TEXT     NOT NULL   pending | done | error
pinned     INTEGER  DEFAULT 0  1=always inject, 0=default, -1=always exclude
adhoc      INTEGER  DEFAULT 0  1=adhoc turn, 0=session turn
tools      TEXT                JSON array of tool_use events (assistant rows only)
created_at TEXT                ISO8601
```

---

### `topic_sessions` — active resumable sessions

One row per `(topic, alias)` pair. Cleared by `/clear` or `DELETE /topics/{topic}/session`.

```
topic       TEXT  PK (composite with alias)
alias       TEXT  PK
session_id  TEXT  NOT NULL    passed to CLI as --resume
cwd         TEXT  NOT NULL    locked at session creation; does not change with agent config updates
created_at  TEXT              ISO8601
```

---

### `session_stats` — per-session token usage

```
session_id           TEXT  PK
topic                TEXT
alias                TEXT    agent alias at time of run
backend              TEXT    resolved backend (claude, codex, …)
model                TEXT    model string reported by CLI
cwd                  TEXT    working directory used
input_tokens         INTEGER
output_tokens        INTEGER
cache_read_tokens    INTEGER
cache_write_tokens   INTEGER
history_input_tokens INTEGER DEFAULT 0   tokens from injected context history
cost_usd             REAL
duration_ms          INTEGER
quota_before         REAL    claude.ai quota at turn start
quota_after          REAL    claude.ai quota at turn end
lookback             INTEGER DEFAULT 0   adhoc lookback window used
pin_count            INTEGER DEFAULT 0   pinned messages injected this turn
created_at           TEXT    ISO8601 — set on INSERT, never updated (use for date bucketing)
```

---

### `session_context_log` — injection tracking

Records which pinned messages have been injected into each `(topic, alias)` session,
preventing double-injection on `--resume`.

```
topic       TEXT  PK (composite)
alias       TEXT  PK
msg_id      INTEGER  PK  FK → chat_messages(id)
injected_at TEXT         ISO8601
```

---

## Key Data Flows

### Session turn (non-adhoc)
1. `POST /chat` resolves alias → looks up agent config → looks up `topic_sessions` for `session_id`
2. `insert_user_message` + `insert_assistant_message` (status=`pending`)
3. CLI spawned via `TopicDispatcher` (FIFO per topic) with `--resume session_id` if present
4. `_stats` chunk arrives → `save_stats()` → `set_topic_session()` → `mark_injected()`
5. `update_assistant_message(status="done")`

### Adhoc turn
Same as above but no `--resume`. Uses `get_context_history(lookback=N)` as inline context.
Session state in `topic_sessions` is not written. `reset_topic_pins()` runs after stats if pins were injected.

### Client disconnect mid-stream
`stream_response` finalizer spawns `_drain_to_completion` as a background task.
The drain processes the remaining queue, captures `_stats` if not yet seen, writes final content, and sets `status="done"`.

### Pin injection
Pinned messages (`pinned=1`) from `chat_messages` are fetched via `get_pending_injections()` and
prepended to the prompt as `inject_history`. After delivery, `mark_injected()` records them in
`session_context_log` so they are not resent on `--resume`.

---

## Open Issues

- **`get_stats_by_agent` collapses all runs under the same alias** even when backend/model changed. Should group by `(alias, backend, model)` and show as separate rows.
- **`alias` vs `agent` naming split**: DB and internal Python use `alias`; HTTP API and UI use `agent`. This is intentional for now — a future pass will rename DB columns.
- **`session_stats.model`** is populated from the `_stats` chunk (reported by CLI). May differ from `aliases.model` if CLI auto-selects a model.
