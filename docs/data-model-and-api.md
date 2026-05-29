# Squid — Data Model & API

## Terminology

| Term | Meaning |
|---|---|
| **agent** | A named configuration: backend, model, cwd, timeout. Defined by the user and stored in the `agents` table. Referenced by name in the `@agent` input syntax. |
| **backend** | The CLI used to run a turn: `claude`, `codex`, `cursor`, `antigravity`, or `copilot`. Must be explicitly set on each agent. |
| **topic** | A named conversation channel (e.g. `oncall`, `backend`). Each topic has a sticky agent you can switch dynamically and zero or more sessions and adhoc turns from multiple agents. Topic = *sessions(*agents) + *adhocs(*agents) |
| **session** | A resumable CLI process context identified by a `session_id` (from `claude --resume`) or `thread_id` (Codex). Scoped to `(topic, agent)`. |)
| **adhoc** | A one-off parallel turn that uses a `lookback` window of recent history as inline context instead of a persistent session. |

---

## Data Model

SQLite database at `squid.db` (project root).

---

### `agents` — agent configurations

```
name       TEXT  PK          user-defined short name (e.g. "clawd", "code")
backend    TEXT  NOT NULL    claude | cursor | antigravity | codex | copilot
model      TEXT              model string (e.g. claude-opus-4-5); null = backend default
cwd        TEXT              working directory; null = /tmp/squid
timeout    INTEGER           per-agent response timeout in seconds; null = global default
created_at TEXT              ISO8601
```

---

### `topics` — topic and agent-level autocomplete summary

Two row types share this table:
- `(topic, '')` — topic-level row: holds `sticky_agent`, `hidden`, `last_prompt` across all agents
- `(topic, 'agentname')` — agent-level row: holds `last_prompt`/`last_at` for that specific agent

```
topic        TEXT  PK (composite with agent)
agent        TEXT  PK  '' = topic-level; agent name = agent-level
sticky_agent TEXT       topic-level only: last-used agent name
last_prompt  TEXT       last user prompt sent
last_at      TEXT       ISO8601 — timestamp of last_prompt
last_model   TEXT       model from agent config at dispatch time
last_backend TEXT       backend from agent config at dispatch time
hidden       INTEGER    1 = soft-deleted (excluded from autocomplete); default 0
created_at   TEXT       ISO8601
```

---

### `chat_messages` — full message history

```
id         INTEGER  PK  AUTOINCREMENT
topic      TEXT     NOT NULL   default: "default"
agent      TEXT                agent name at time of message
session_id TEXT                CLI session_id (set after stats arrive)
role       TEXT     NOT NULL   user | assistant
content    TEXT                message body (null while pending)
reply_to   INTEGER  FK → id    assistant rows point to their user message
status     TEXT     NOT NULL   pending | done | error
adhoc      INTEGER  DEFAULT 0  1=adhoc turn, 0=session turn
context    TEXT                user rows: JSON array of context message IDs [id, …]
                               assistant rows: JSON array of tool_use events
created_at TEXT                ISO8601
```

---

### `topic_sessions` — active resumable sessions

One row per `(topic, agent)` pair. Cleared by `/clear` or `DELETE /topics/{topic}/session`.

```
topic       TEXT  PK (composite with agent)
agent       TEXT  PK
session_id  TEXT  NOT NULL    passed to CLI as --resume
cwd         TEXT  NOT NULL    locked at session creation; does not change with agent config updates
created_at  TEXT              ISO8601
```

---

### `session_stats` — per-session token usage

```
session_id           TEXT  PK
topic                TEXT
agent                TEXT    agent name at time of run
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
created_at           TEXT    ISO8601 — set on INSERT, never updated (used for date bucketing)
```

---

## Key Data Flows

### Session turn (non-adhoc)
1. `POST /chat` resolves agent name → looks up agent config → looks up `topic_sessions` for `session_id`
2. `insert_user_message` (with `context=[]`) + `insert_assistant_message` (status=`pending`)
3. CLI spawned via `TopicDispatcher` (FIFO per topic) with `--resume session_id` if present
4. If `--resume` fails with "No conversation found": emit `status` event with stale session details, retry as fresh (see ADR-0001 — Stale Session Recovery)
5. `_stats` chunk arrives → `save_stats()` → `set_topic_session()`
6. `update_assistant_message(status="done", context=<tool_events_json>)`

### Adhoc turn
Same as above but no `--resume`. Uses `get_context_history(lookback=N)` as inline context
(last N non-adhoc session turns for the topic/agent pair).
The returned message IDs are stored in the user message's `context` column.
Session state in `topic_sessions` is not written.

### Client disconnect mid-stream
`stream_response` finalizer spawns `_drain_to_completion` as a background task.
The drain processes the remaining queue, captures `_stats` if not yet seen, writes final content, and sets `status="done"`.

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
  "agent":    "string | null  — agent name; null = use topic sticky",
  "lookback": "integer (default: 0) — adhoc context window",
  "adhoc":    "boolean (default: false)"
}
```

**SSE event stream** (in order)

| Event | Data | When |
|---|---|---|
| `meta` | `{"agent": str\|null, "backend": str, "msg_id": int, "adhoc": bool}` | First, always |
| `queued` | `{"topic": str, "position": int}` | When behind another turn in the topic queue |
| `status` | status line text | When CLI emits non-content output; also emitted for stale session recovery (see below) |
| `tool` | `{"type": str, "name": str, ...}` | When backend calls a tool |
| `data:` (chunk) | raw text fragment | During content streaming |
| `stats` | see Stats object below | On session completion |
| `done` | _(empty)_ | Normal completion |
| `error` | error message string | On CLI error |

**Stale session recovery**: If `--resume` fails with "No conversation found" (e.g. after a
reboot changes the resolved `cwd`), Squid emits a `status` event describing the lost session
(`session_id`, `cwd`, `backend`, `model`) then retries the prompt as a fresh turn. The client
receives a normal `done`/`stats` at the end. See ADR-0001.

**Client note — deferred response bubble**: The built-in UI withholds the response bubble
from the DOM until the `done` event fires, then appends it at the bottom of the message list.
During streaming, content is shown as a live plain-text preview inside the thinking bubble.
Clients building their own UI should expect all `data:` chunks before `done`, then render the
full response once. See ADR-0011.

**Stats object** (sent in the `stats` event and stored in `session_stats`)
```json
{
  "session_id":            "string",
  "input_tokens":          0,
  "output_tokens":         0,
  "cache_read_tokens":     0,
  "cache_write_tokens":    0,
  "history_input_tokens":  0,
  "reasoning_tokens":      0,
  "cost_usd":              0.0,
  "duration_ms":           0,
  "adhoc":                 false,
  "lookback":              0,
  "cwd":                   "/path/to/cwd"
}
```

`reasoning_tokens` — Codex only; reflects `reasoning_output_tokens` from the Codex response.
Zero for all other backends.

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
{ "ok": true, "killed": true }                 // stop
{ "ok": true, "killed": int, "drained": int }  // stopall
{ "ok": true, "drained": int }                 // deq
{ "ok": true, "agent": "agent-name" }          // clear / compact
{ "ok": true, "topics": [...] }                // list
{ "ok": false, "error": "..." }                // 400
```

---

### GET /history

**Query params**: `topic`, `agent`, `offset` (default 0), `limit` (default 5)

**Response**
```json
{
  "items": [
    {
      "id":         1,
      "role":       "user | assistant",
      "topic":      "string",
      "agent":      "string | null",
      "session_id": "string | null",
      "content":    "string | null",
      "status":     "done | pending | error",
      "adhoc":      0,
      "context":    "json string | null",
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
    "agent":        "string | null  — sticky agent",
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

### POST /topics/{topic}/hide

Soft-delete a topic — hides it from autocomplete (`hidden=1`). Topic reappears automatically if a new message is sent to it.

**Response**: `{ "ok": true | false }`

---

### DELETE /topics/{topic}

Hard-delete a topic. Removes all topic rows, topic sessions, chat messages, and session stats. Irreversible.

**Response**: `{ "ok": true | false }`

---

### GET /topics/{topic}/agents/history

Returns agents previously used in a topic, ordered by most recent use. Used to populate `#topic@agent` autocomplete.

**Response**:
```json
[{ "agent": "clawd", "last_prompt": "fix the auth bug", "last_at": "ISO8601" }]
```

---

### GET /topics/{topic}/sessions

Returns all agents with active sessions for a topic.

**Response**: `{ "agents": [{ "agent": str, "session_id": str, "cwd": str, "created_at": str }] }`

---

### GET /topics/{topic}/session?agent={agent}

Returns the active session state for a `(topic, agent)` pair.

**Response**
```json
{
  "session_id": "string | null",
  "cwd":        "string | null"
}
```

---

### DELETE /topics/{topic}/session?agent={agent}

Clears the active session for `(topic, agent)`. Next message starts fresh.

**Response**: `{ "ok": true }`

---

### GET /context/{topic}?agent={agent}

Same as `GET /topics/{topic}/session`.

---

### GET /chat/{msg_id}/status

Poll a single message for status (used when client reconnects mid-stream).

**Response**: `{ "id": int, "status": "pending | done | error", "content": str | null }`

---

### GET /config/agents

**Response** — array of agent configs:
```json
[
  {
    "name":       "string",
    "backend":    "claude | cursor | antigravity | codex | copilot",
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
  "backend": "claude | cursor | antigravity | codex | copilot",
  "model":   "string | null",
  "cwd":     "string | null  — abs path; null = /tmp/squid",
  "timeout": "integer | null  — seconds"
}
```

**Response**:
```json
{ "ok": true }
{ "ok": true, "sessions_cleared": ["topic1", "topic2"] }  // if key attrs changed
```

Key attributes: `backend`, `model`, `cwd`. Changing any of these clears existing topic sessions so the next turn starts a fresh CLI session.

---

### DELETE /config/agents/{name}

Delete an agent and its topic sessions.

**Response**: `{ "ok": true | false }`

---

### GET /stats

**Query params**: `period` (`daily` | `hourly`, default `daily`), `group` (`time` | `topic` | `agent`, default `time`)

**Response — group=time**
```json
[
  {
    "period":             "2026-05-28",
    "sessions":           12,
    "input_tokens":       48000,
    "new_input_tokens":   10000,
    "output_tokens":      6000,
    "cache_read_tokens":  30000,
    "cache_write_tokens": 8000,
    "cost_usd":           0.42,
    "quota_delta":        -0.05
  }
]
```

`new_input_tokens` = `input_tokens - history_input_tokens` — net new tokens excluding
injected adhoc context history. Useful for understanding actual prompt cost vs. re-injected context cost.

**Response — group=topic**
```json
[{ "topic": "work", "sessions": 5, "input_tokens": 20000, "output_tokens": 3000, "cost_usd": 0.18 }]
```

**Response — group=agent**
```json
[{ "agent": "clawd", "sessions": 8, "input_tokens": 32000, "output_tokens": 5000, "cache_read_tokens": 20000, "cache_write_tokens": 5000, "cost_usd": 0.30 }]
```

Note: `agent` is `COALESCE(agent, backend, 'unknown')` — groups all sessions under the same agent name regardless of which backend/model was active at the time.

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

Fetch current claude.ai usage. Requires saved credentials.

**Response**: proxied JSON from claude.ai, or `{ "error": "..." }` (400/502).

---

### GET /processes

**Response** — array of active CLI processes:
```json
[{ "pid": 1234, "backend": "claude", "topic": "work", "agent": "clawd", "duration_s": 4.2, "started_iso": "ISO8601" }]
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

## Open Issues

- **`get_stats_by_agent` collapses all runs under the same agent name** even when backend/model changed. Should group by `(agent, backend, model)` and show as separate rows.
- **`session_stats.model`** is populated from the `_stats` chunk (reported by CLI). May differ from `agents.model` if the CLI auto-selects a model.
