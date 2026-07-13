# Squid — Data Model & API

## Terminology

| Term | Meaning |
|---|---|
| **harness** | The coded coding-agent CLI integration: `claudecode`, `codex`, `cursor`, `opencode`, or `pi`. A harness owns command construction, supported protocols, parsing, resume behavior, and token semantics. |
| **provider** | A model endpoint/account: label, color, base URL, auth/API key, quota gauge, model suggestions, and env/settings/args escapes. |
| **agent** | A named execution identity: harness, provider, model override, cwd, timeout. Defined by the user and stored in the `agents` table. Referenced by name in the `@agent` input syntax. |
| **topic** | A named conversation/work thread (e.g. `oncall`, `squid`). Each topic has a sticky agent and zero or more sessions and adhoc turns from multiple agents. |
| **route** | A topic plus agent selection written as `#topic@agent`. `#topic` owns conversation history; `@agent` owns execution config. |
| **protocol** | The harness communication shape for a turn/session, such as `oneshot-cli`, `interactive-cli`, or `interactive-pty`. Protocol selection is harness configuration, not model-name inference. |
| **session** | A resumable CLI process context identified by the native CLI session/thread ID. Scoped to `(topic, agent)`. |
| **adhoc** | A one-off parallel turn that uses a `lookback` window of recent history as inline context instead of a persistent session. |

---

## Data Model

SQLite database at `~/.squid/squid.db` (persists across installs).

---

### `agents` — agent configurations

```
name       TEXT  PK          user-defined short name (e.g. "clawd", "code")
harness    TEXT              coded CLI integration; preferred source of truth
provider   TEXT              provider id; null = harness default provider
model      TEXT              model string (e.g. claude-opus-4-5); null = provider/harness default
cwd        TEXT              working directory; null = /tmp/<user>/squid
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
sticky_adhoc INTEGER    topic-level only: last-selected mode; default 0
last_prompt  TEXT       last user prompt sent
last_adhoc_prompt TEXT  last adhoc user prompt sent
last_at      TEXT       ISO8601 — timestamp of last_prompt
last_model   TEXT       model from agent config at dispatch time
last_harness TEXT       harness from agent config at dispatch time
last_provider TEXT      provider from agent config at dispatch time
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
runtime_fingerprint TEXT      harness/provider/protocol execution fingerprint
created_at  TEXT              ISO8601
```

---

### `session_stats` — per-session token usage

```
session_id           TEXT  PK
topic                TEXT
agent                TEXT    agent name at time of run
harness              TEXT    harness used for the run
provider             TEXT    provider used for the run
model                TEXT    model string reported by CLI
cwd                  TEXT    working directory used
input_tokens         INTEGER
output_tokens        INTEGER
cache_read_tokens    INTEGER
cache_write_tokens   INTEGER
history_input_tokens INTEGER DEFAULT 0   tokens from injected context history
cost_usd             REAL
duration_ms          INTEGER
quota_before         REAL    observed provider-wide quota percentage at turn start
quota_after          REAL    observed provider-wide quota percentage after turn completion
adhoc                INTEGER DEFAULT 0   1=adhoc turn, 0=session turn
lookback             INTEGER DEFAULT 0   adhoc lookback window used
created_at           TEXT    ISO8601 — set on INSERT, never updated (used for date bucketing)
```

---

## Key Data Flows

### Session turn (non-adhoc)
1. `POST /chat` resolves route (`#topic@agent` or sticky topic) → looks up agent config → resolves harness + provider → resolves protocol → looks up `topic_sessions` for `session_id`
2. `insert_user_message` (with `context=[]`) + `insert_assistant_message` (status=`pending`)
3. CLI spawned via `TopicDispatcher` (FIFO per topic) with `--resume session_id` if present
4. If `--resume` fails with "No conversation found": emit `status` event with stale session details, retry as fresh (see ADR-0001 — Stale Session Recovery)
5. `_stats` chunk arrives → `save_stats()` → `set_topic_session()`
6. `update_assistant_message(status="done", context=<tool_events_json>)`

### Adhoc turn
Same as above but no `--resume`. Uses `get_context_history(lookback=N)` as inline context
(last N non-adhoc session turns for the topic/agent pair).
The returned message IDs are stored in the user message's `context` column.
Session state in `topic_sessions` is not written. Adhoc turns force
`oneshot-cli` because they are parallel and outside the durable `(topic, agent)`
session queue.

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
| `meta` | `{"agent": str\|null, "harness": str, "provider": str\|null, "model": str\|null, "msg_id": int, "adhoc": bool}` | First, always |
| `queued` | `{"topic": str, "position": int}` | When behind another turn in the topic queue |
| `status` | status line text | When CLI emits non-content output; also emitted for stale session recovery (see below) |
| `tool` | `{"type": str, "name": str, ...}` | When the harness/provider runtime calls a tool |
| `data:` (chunk) | raw text fragment | During content streaming |
| `stats` | see Stats object below | On session completion |
| `done` | _(empty)_ | Normal completion |
| `error` | error message string | On CLI error |

**Stale session recovery**: If `--resume` fails with "No conversation found" (e.g. after a
reboot changes the resolved `cwd`), Squid emits a `status` event describing the lost session
(`session_id`, `cwd`, `harness`, `provider`, `model`) then retries the prompt as a fresh turn. The client
receives a normal `done`/`stats` at the end. See ADR-0001.

**Client note — deferred response bubble**: The built-in UI withholds the response bubble
from the DOM until the `done` event fires, then appends it at the bottom of the message list.
During streaming, content is shown as a live plain-text preview inside the thinking bubble.
Clients building their own UI should expect all `data:` chunks before `done`, then render the
full response once. See ADR-0011.

**Stats object** (sent in the `stats` event; most fields are stored in `session_stats`)
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
Zero for all other harnesses. **Not stored in `session_stats`** — SSE event only.

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
  "command": "stop | stopall | deq | list | restart | clear | stop_msg",
  "topic":   "string (default: \"default\")",
  "agent":   "string | null  — scopes stop/stopall to one agent lane; required for clear if no sticky",
  "adhoc":   "boolean | null — scopes stop/stopall to adhoc-only turns",
  "pos":     "integer | null  — deq only: null=all, 1=first, -1=last",
  "msg_id":  "integer | null  — stop_msg only: kill the process running this message"
}
```

**Response**
```json
{ "ok": true }
{ "ok": true, "killed": true }                 // stop
{ "ok": true, "killed": int, "drained": int }  // stopall
{ "ok": true, "drained": int }                 // deq
{ "ok": true, "agent": "agent-name" }          // clear
{ "ok": true, "topics": [...] }                // list
{ "ok": true, "killed": int }                  // stop_msg
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
    "last_harness": "string | null",
    "last_provider": "string | null",
    "last_backend": "string | null",
    "last_prompt":  "string | null",
    "last_at":      "ISO8601 | null",
    "queue_depth":  0,
    "active":       false
  }
]
```

---

### GET /topics/manage

Returns topic management data for the Topics tab. Unlike `GET /topics`, this includes hidden topics by default and nests the agent/adhoc lane summary under each topic.

**Query params**: `include_hidden` (default `true`)

**Response**:
```json
[
  {
    "name": "squid",
    "agent": "codex",
    "hidden": false,
    "last_prompt": "implement the topic manager",
    "queue_depth": 0,
    "active": false,
    "memory": { "exists": true, "path": "~/.squid/context/topics/squid/memory.md" },
    "agents": [
      {
        "agent": "codex",
        "last_prompt": "session prompt",
        "last_adhoc_prompt": "adhoc prompt"
      }
    ]
  }
]
```

---

### PUT /topics/{topic}/hidden

Sets whether a topic is hidden from autocomplete without deleting its messages or sessions.

**Request body**: `{ "hidden": true | false }`

**Response**: `{ "ok": true | false, "hidden": true | false }`

---

### DELETE /topics/{topic}

Delete a topic from active workflow state. Removes topic rows, topic sessions,
chat messages, and associated message search rows. Retains `session_stats` so
Squid-attributed consumption history remains stable. Irreversible for deleted
chat history; retained aggregate stats may still appear in Stats filters and
totals. See ADR-0029.

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
    "harness":    "claudecode | codex | cursor | opencode | pi",
    "provider":   "provider ID | null",
    "provider_label": "string",
    "provider_color": "#RRGGBB",
    "model":      "string | null",
    "cwd":        "string | null",
    "timeout":    300,
    "created_at": "ISO8601"
  }
]
```

---

### POST /config/agents

Create or update an agent (upsert by name). An agent is the named execution
identity used in `#topic@agent`: harness, provider, model override, cwd, and timeout.

**Request body**
```json
{
  "name":     "string (required)",
  "harness":  "claudecode | codex | cursor | opencode | pi",
  "provider": "provider ID | null",
  "model":    "string | null",
  "cwd":      "string | null  — abs path; null = /tmp/<user>/squid",
  "timeout":  "integer | null  — seconds"
}
```

**Response**:
```json
{ "ok": true }
{ "ok": true, "sessions_cleared": ["topic1", "topic2"] }  // if key attrs changed
```

Key attributes: `harness`, `provider`, `model`, `cwd`. Changing any of these clears existing topic sessions so the next turn starts a fresh CLI session.

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

`quota_delta` is an observational estimate (`quota_after - quota_before`), not
per-prompt attribution. Concurrent prompts on the same provider have overlapping
measurement windows and can double-count usage when these values are summed.
Provider reporting delay can also shift usage into a later observation. See
[ADR-0023](decisions/0023-quota-deltas-are-observational.md).

**Response — group=topic**
```json
[{ "topic": "work", "sessions": 5, "input_tokens": 20000, "output_tokens": 3000, "cost_usd": 0.18 }]
```

**Response — group=agent**
```json
[{ "agent": "clawd", "sessions": 8, "input_tokens": 32000, "output_tokens": 5000, "cache_read_tokens": 20000, "cache_write_tokens": 5000, "cost_usd": 0.30 }]
```

Note: `agent` is currently the stored agent name, falling back to the computed runtime ref when an agent name is absent.

---

### POST /stats/quota-delta

Record observed before/after provider-wide quota percentages for a session. The
UI records this only for providers with quota integrations. A later write for the
same `session_id` replaces the stored pair; this endpoint does not accumulate a
per-session ledger. Values can include other concurrent prompts and must not be
treated as exact per-prompt consumption. See
[ADR-0023](decisions/0023-quota-deltas-are-observational.md).

**Request body**: `{ "session_id": "string", "before": 0.0, "after": 0.0 }`

**Response**: `{ "ok": true }`

---

### POST /config/creds

Save claude.ai session credentials (org ID + session key).

**Request body**: `{ "org_id": "string", "session_key": "string" }`

**Response**: `{ "ok": true }`

---

### GET /quota/claude

Fetch current claude.ai usage. Requires saved credentials.

**Response**: proxied JSON from claude.ai, or `{ "error": "..." }` (400/502).

### GET /quota

Legacy alias for `GET /quota/claude`.

### GET /quota/codex

Fetch current Codex usage. Requires a saved Codex bearer token.

**Response**: proxied Codex account usage JSON, or `{ "error": "..." }` (400/502).

---

### GET /processes

**Response** — array of active CLI processes:
```json
[{ "pid": 1234, "backend": "claudecode:anthropic", "topic": "work", "agent": "clawd", "duration_s": 4.2, "started_iso": "ISO8601" }]
```

---

### GET /health

**Response**
```json
{
  "status":    "ok",
  "boot_time": "ISO8601",
  "squid_home": "/Users/me/.squid",
  "harnesses": [
    {
      "id": "claudecode",
      "label": "Claude Code",
      "install_cmd": "curl -fsSL https://claude.ai/install.sh | bash",
      "installed": true,
      "protocol": "interactive-cli",
      "interactive": { "idle_timeout_seconds": 3600 },
      "default_provider": "anthropic",
      "supported_apis": ["/v1/messages"],
      "compatible_providers": ["anthropic", "deepseek"]
    }
  ],
  "providers": {
    "anthropic": {
      "label": "Claude",
      "color": "#AE5332",
      "base_url": null,
      "auth_type": "subscription",
      "missing_secrets": [],
      "gauge": { "type": "claude", "text": null, "title": null },
      "gauge_authed": true,
      "models": ["claude-sonnet-4-6"],
      "supported_apis": ["/v1/messages"]
    }
  }
}
```

`harnesses` and `providers` are the runtime catalogs. Harnesses describe CLI
integrations; providers describe endpoint/account metadata, auth, models, and
quota gauges.

`available` means the harness executable and configured provider secret
references are present. It does not probe the provider endpoint.

### GET /quota/provider/{provider_id}

Returns a normalized dynamic or static gauge snapshot for one configured
provider. Gauge routing and credentials come from provider config and are
independent of which harness is using that provider.

```json
{ "status": "ok", "text": "$12.34", "raw": 12.34, "used_percent": null, "reset_at": null, "title": "DeepSeek balance" }
```

---

## Open Issues

- **`get_stats_by_agent` collapses all runs under the same agent name** even when harness/provider/model changed. Should group by `(agent, harness, provider, model)` and show as separate rows.
- **`session_stats.model`** is populated from the `_stats` chunk (reported by CLI). May differ from `agents.model` if the CLI auto-selects a model.
