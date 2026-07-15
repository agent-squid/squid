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
| **worktree** | An isolated git worktree checked out per `(topic, agent, repo)` so parallel turns don't collide on the same working tree. Tracked in the `worktrees` table. |

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
last_session_at TEXT    ISO8601 — timestamp of last non-adhoc (session) prompt
last_adhoc_at   TEXT    ISO8601 — timestamp of last adhoc prompt
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
session_id TEXT                CLI session_id (known resume id at dispatch; otherwise set after stats arrive)
role       TEXT     NOT NULL   user | assistant
content    TEXT                message body (null while pending)
reply_to   INTEGER  FK → id    assistant rows point to their user message
status     TEXT     NOT NULL   pending | done | error | cancelled
adhoc      INTEGER  DEFAULT 0  1=adhoc turn, 0=session turn
context    TEXT                user rows: JSON array of context message IDs [id, …]
                               assistant rows: JSON array of tool_use events
status_raw TEXT                raw harness status/debug text
session_turn_index INTEGER     ordinal assistant turn within a non-adhoc session
lookback   INTEGER  DEFAULT 0  adhoc lookback window used for this turn
quota_delta  REAL              quota_after - quota_before, if recorded
quota_before REAL              per-message quota snapshot (see POST /chat/{msg_id}/quota-delta)
quota_after  REAL              per-message quota snapshot
completed_at TEXT              ISO8601 — set when status becomes done|error|cancelled
created_at TEXT                ISO8601
```

`cancelled` is written by `mark_assistant_cancelled` at stop-request time
(before the process is killed), not inferred afterward, and only ever applies
to a still-`pending` row. See [ADR-0033](decisions/0033-cancelled-and-error-turn-capture.md).
For cancelled assistant rows, Squid reconstructs durable partial response,
status trace, tool context, and stats-derived `session_id` from `run_events`.
Pinned prompt context and memory metadata remain on the linked user row's
`context` field and are exposed as `prompt_context`.

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

### `run_events` — per-message SSE event log

Replayable log of every SSE event emitted for a message, keyed by `(msg_id, seq)`.
Backs `GET /chat/{msg_id}/events` for reconnecting clients.

```
id         INTEGER  PK  AUTOINCREMENT
msg_id     INTEGER  NOT NULL   chat_messages.id (not FK-enforced)
seq        INTEGER  NOT NULL   monotonic per msg_id
event_type TEXT     NOT NULL   text | tool | status | stats | done | error
payload    TEXT                event data
created_at TEXT                ISO8601
UNIQUE(msg_id, seq)
```

---

### `git_diff_reverts` — reverted-file ledger

Tracks which files from a captured GitDiff tool call have already been
reverted, so `POST /chat/{msg_id}/revert` doesn't double-apply.

```
msg_id      INTEGER  PK (composite)
repo        TEXT     PK (composite)
file_path   TEXT     PK (composite)
reverted_at TEXT     ISO8601
```

---

### `file_edit_history` — localfile editor undo log

Before/after snapshots for edits made via `POST /localfile`. Backs
`GET /localfile/history` and `POST /localfile/revert-edit`.

```
id        INTEGER  PK  AUTOINCREMENT
file_path TEXT     NOT NULL
before    TEXT     NOT NULL
after     TEXT     NOT NULL
edited_at TEXT      ISO8601
```
Indexed on `(file_path, id DESC)`.

---

### `bookmarks` — saved messages

```
msg_id   INTEGER  PK
topic    TEXT
agent    TEXT
content  TEXT
saved_at TEXT     ISO8601
```

---

### `worktrees` — per-(topic, agent, repo) git worktree isolation

One row per repo a topic/agent has an isolated worktree in. Cleaned up on
session clear; see `agent/worktree.py`.

```
topic         TEXT  PK (composite)
agent         TEXT  PK (composite)
repo_root     TEXT  PK (composite)
worktree_path TEXT  NOT NULL
branch_name   TEXT  NOT NULL
status        TEXT  NOT NULL  DEFAULT 'active'
created_at    TEXT            ISO8601
last_used_at  TEXT            ISO8601
```

---

### `stats_filter_presets` — saved Stats-tab filter presets

```
id            INTEGER  PK  AUTOINCREMENT
name          TEXT     NOT NULL  UNIQUE (case-insensitive)
state_json    TEXT     NOT NULL  serialized filter state
is_default    INTEGER  NOT NULL  DEFAULT 0  (unique when 1, i.e. at most one default)
display_order INTEGER  NOT NULL  DEFAULT 0
created_at    TEXT     NOT NULL  ISO8601
updated_at    TEXT     NOT NULL  ISO8601
```

---

### `messages_fts` / `prompts_fts` — full-text search indexes

Standalone SQLite FTS5 tables kept in sync with `chat_messages` via triggers
(not external-content tables). `messages_fts` indexes final assistant content
(synced on the `status='done'` update); `prompts_fts` indexes user prompts
(synced on insert). Back `GET /search`. See ADR-0021.

---

## Key Data Flows

### Session turn (non-adhoc)
1. `POST /chat` resolves route (`#topic@agent` or sticky topic) → looks up agent config → resolves harness + provider → resolves protocol → looks up `topic_sessions` for `session_id`
2. `insert_user_message` (with `context=[]`) + `insert_assistant_message` (status=`pending`), then attach any known resumed `session_id`
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

Stream an AI response. Returns `text/event-stream` with response header
`X-Squid-Msg-Id` (the assistant message id, available before the stream body
completes).

**Request body**
```json
{
  "message":  "string (required)",
  "topic":    "string (default: \"default\")",
  "agent":    "string | null  — agent name; null = use topic sticky",
  "lookback": "integer (default: 0) — adhoc context window",
  "lookback_via_pins": "boolean (default: false) — use pinned_ids instead of last-N history",
  "adhoc":    "boolean (default: false)",
  "pinned_ids": "integer[] | null — explicit message IDs to inject as context when lookback_via_pins",
  "include_topic_memory": "boolean (default: false) — inject the topic's memory.md as context"
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
  "command": "stop | stopall | deq | list | restart | clear | stop_msg | journal",
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
{ "ok": true, "agent": "agent-name", "worktree_conflicts": [...] }  // clear
{ "ok": true, "topics": [...] }                // list
{ "ok": true, "killed": int }                  // stop_msg
{ "ok": true, "file": "path" }                 // journal
{ "ok": false, "error": "..." }                // 400
```

---

### GET /processes

**Response** — array of active CLI processes:
```json
[{ "pid": 1234, "backend": "claudecode:anthropic", "topic": "work", "agent": "clawd", "duration_s": 4.2, "started_iso": "ISO8601" }]
```

---

### GET /queue

**Response** — all queued (not-yet-dispatched) turns across all topics, from `TopicDispatcher.all_queued_items()`.

---

### GET /health

**Response**
```json
{
  "status":    "ok",
  "boot_time": "ISO8601",
  "version":   "string",
  "squid_home": "/Users/me/.squid",
  "total_prompts": 0,
  "first_seen": "ISO8601 | null",
  "harnesses": [
    {
      "id": "claudecode",
      "label": "Claude Code",
      "install_cmd": "curl -fsSL https://claude.ai/install.sh | bash",
      "installed": true,
      "protocol": "oneshot-cli",
      "interactive": { "idle_timeout_seconds": 0 },
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
quota gauges. `available` means the harness executable and configured
provider secret references are present; it does not probe the provider
endpoint.

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

### GET /history/by-ids?ids={csv}

Fetch specific message rows by id (comma-separated, capped at 200), same row shape as `GET /history` items.

**Response**: `{ "items": [...] }`

---

### GET /search

Full-text search over messages via `messages_fts`/`prompts_fts`.

**Query params**: `q` (required), `limit` (default 100), `topic`, `agent`, `adhoc`, `role` (default `"assistant"`), `bookmarked` (default `false`)

**Response**: `{ "items": [...] }`

---

### GET /prompts/recent

**Query params**: `limit` (default 50, capped 200)

**Response**: `{ "items": [...recent user prompts...], "agents": {...public agent configs by name...} }`

---

### GET /chat/previews?ids={csv}

Short content previews for a set of message IDs (comma-separated, capped at 200).

**Response**: `{ "items": [...] }`

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

### GET /topics/{topic}/memory

Returns the topic's memory document (frontmatter-parsed `memory.md`).

### PUT /topics/{topic}/memory

**Request body**: `{ "content": "string" }`

**Response**: result of writing the topic memory file.

### PUT /topics/{topic}/memory/squid/code-roots

Updates the `squid.code_roots` list in the topic's memory frontmatter (see the
`<squid_code_roots>` block injected into this conversation).

**Request body**: `{ "code_roots": ["string", ...], "code_roots_skipped": false }`

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
  "session_id":      "string | null",
  "cwd":             "string | null",
  "injected_ids":    [1, 2],
  "memory_injected": false,
  "memory_revision": "string | null"
}
```

---

### DELETE /topics/{topic}/agent?agent={agent}&adhoc={bool}

Deletes a topic/agent's messages and sessions. If `adhoc` is given, scopes
deletion to adhoc-only or session-only turns.

**Response**: `{ "ok": true }`

---

### DELETE /topics/{topic}/session?agent={agent}

Clears the active session for `(topic, agent)`. Next message starts fresh. Also cleans up any associated worktrees.

**Response**: `{ "ok": true, "worktree_conflicts": [...] }`

---

### GET /context/{topic}?agent={agent}

Same as `GET /topics/{topic}/session`.

---

### GET /chat/{msg_id}/status

Poll a single message for status (used when client reconnects mid-stream).
Recovers content from the `run_events` log if the row is still `pending` but
the run actually completed.

**Response**: `{ "id": int, "status": "pending | done | error", "content": str | null }`

---

### GET /chat/{msg_id}/events?after_seq={int}

Replays `run_events` for a message as `text/event-stream` (text/stats/status/tool/done/error),
polling every 0.5s until the message reaches a terminal status. Used by
clients reconnecting mid-stream.

---

### GET /config/agents/{name}/sessions

Returns all active `topic_sessions` rows for an agent, across topics.

**Response**: `{ "topics": [...] }`

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

### GET /config/yaml

Same-origin only. Returns the raw config YAML text for the built-in config editor.

**Response**: `{ "content": "string", "revision": "string", "path": "string" }`

### PUT /config/yaml

Same-origin only. Validates and writes the config YAML. Uses `revision` for
optimistic concurrency — 409 if the file changed since it was read.

**Request body**: `{ "content": "string", "revision": "string | null" }`

**Response**: `{ "ok": true, "revision": "string", "restart_required": bool, "backup": "path" }`

---

### GET /config/localfile-roots

Same-origin only. Returns the current allowed localfile roots.

**Response**: `{ "roots": [...] }`

### POST /config/localfile-roots

Same-origin only. Appends a new allowed root for `GET/POST /localfile`.

**Request body**: `{ "path": "string", "root": "string" }`

**Response**: `{ "ok": true, "root": "string", "added": bool }`

---

### GET /stats/filters

**Response**: available filter dimensions/values for the Stats tab.

---

### GET /stats/filter-presets

**Response**: saved `stats_filter_presets` rows.

### POST /stats/filter-presets

**Request body**: `{ "name": "string", "state": {...}, "is_default": bool }`

**Response**: created preset, or `{ "error": "..." }` (400 on duplicate name).

### PUT /stats/filter-presets/{preset_id}

**Request body**: same as POST. **Response**: updated preset, 404 if missing.

### DELETE /stats/filter-presets/{preset_id}

**Response**: `{ "ok": true }`

---

### GET /stats

**Query params**: `period` (`daily` | `hourly`, default `daily`), `group` (`time` | `topic` | `agent`, default `time`),
`breakdown` (default `""`), `days` (default 30), `hours` (default 0), `agent` (default `""`),
`topic` (default `""`), `adhoc` (`all` | `true` | `false`, default `all`), `tz_offset_minutes` (default 0),
`chart_metrics`, `chart_aggs` (comma-separated), `anchor`.

The response shape varies by `group`/`breakdown` combination — turn-level
stats, time-bucketed breakdowns, by-topic, by-agent, or aggregated
chart-ready time-series. The three documented shapes below (`group=time`,
`group=topic`, `group=agent`) are the common cases.

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

### POST /chat/{msg_id}/quota-delta

Record a per-message quota before/after snapshot (distinct from the
session-level `POST /stats/quota-delta`). Stored in `chat_messages.quota_before`/`quota_after`/`quota_delta`.

**Request body**: `{ "before": 0.0, "after": 0.0 }`

**Response**: `{ "ok": true }`

---

### GET /chat/{msg_id}/diff-revert-status?repo={repo}

Returns per-file revert eligibility for a captured GitDiff tool call on this
message (checked against `git_diff_reverts`). 400 on an invalid repo path,
404 if no diff was captured for this message.

---

### POST /chat/{msg_id}/revert

Reverse-applies a git diff captured from an earlier GitDiff tool call, for one
file or (if `file_path` omitted) all files in the diff. Recorded in
`git_diff_reverts` to prevent double-reverting.

**Request body**: `{ "repo": "string", "file_path": "string | null" }`

**Response**: `{ "ok": true, "reverted": [...], "failed": [...] }`

---

### GET /bookmarks

**Response**: `{ "items": [...] }` — all saved bookmarks.

### POST /bookmarks

**Request body**: `{ "msg_id": int, "topic": "string | null", "agent": "string | null", "content": "string | null" }`

**Response**: `{ "ok": true }`

### DELETE /bookmarks/{msg_id}

**Response**: `{ "ok": true }`

---

### POST /config/creds

Save claude.ai session credentials (org ID + session key).

**Request body**: `{ "org_id": "string", "session_key": "string" }`

**Response**: `{ "ok": true }`

### POST /config/creds/auto

Auto-detect Claude web credentials from local Chrome cookies. No request body.

**Response**: `{ "ok": true, "claude_org_id": "string" }` or `{ "error": "..." }`

### POST /config/creds/codex/auto

Auto-detect a Codex bearer token from the local Codex CLI install. No request body.

**Response**: `{ "ok": true }`

### POST /config/creds/codex

Save a Codex bearer token directly.

**Request body**: `{ "token": "string" }`

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

### GET /quota/cursor

Fetch current Cursor usage.

**Response**: proxied cursor.sh usage-summary JSON, or `{ "error": "..." }` (400/502).

### GET /quota/deepseek

Fetch current DeepSeek account balance.

**Response**: proxied DeepSeek balance JSON, or `{ "error": "..." }` (400/502).

---

### POST /config/deepseek/max-budget

Set a soft spend cap used by the DeepSeek quota gauge.

**Request body**: `{ "amount": 0.0 }`

**Response**: `{ "status": "ok" }` or 400.

### DELETE /config/deepseek/max-budget

Clear the DeepSeek spend cap.

**Response**: `{ "status": "ok" }`

---

### GET /quota/provider/{provider_id}

Returns a normalized dynamic or static gauge snapshot for one configured
provider. Gauge routing and credentials come from provider config and are
independent of which harness is using that provider. Internally delegates to
the claude/codex/cursor/deepseek/static logic above based on the provider's
gauge type.

```json
{ "status": "ok", "text": "$12.34", "raw": 12.34, "used_percent": null, "reset_at": null, "title": "DeepSeek balance", "seven_day": null, "max_budget": null }
```

---

### GET /journals/{topic}

**Response**: list of available weekly journal entries for a topic.

### GET /journals/{topic}/{week}?agent={agent}

**Response**: `text/markdown` journal content for that topic/week. 404 if not found.

---

### GET /remote

Returns the Tailscale HTTPS URL for this Squid instance, via `tailscale status --json`.

**Response**: `{ "url": "string" }` or `{ "url": null, "reason": "string" }`

---

### GET /localfile?path={path}&render={bool}

Read a file or list a directory, gated by same-origin requests and the
`server.localfile_roots` allowlist (see `PUT/POST /config/localfile-roots`).
Returns a JSON directory listing, rendered HTML (for markdown when
`render=true`), plain text, or a raw file response depending on path/params.

### POST /localfile

Write a file under an allowed root. Records a before/after snapshot in
`file_edit_history`.

**Request body**: `{ "path": "string", "content": "string" }`

**Response**: `{ "ok": true, "edit_id": int }`

### GET /localfile/history?path={path}

**Response**: `{ "history": [...] }` — edit history rows for a path, from `file_edit_history`.

### POST /localfile/revert-edit

Revert a file to the `before` snapshot of a given edit.

**Request body**: `{ "edit_id": int }`

**Response**: `{ "ok": true }`

---

## Open Issues

- **`get_stats_by_agent` collapses all runs under the same agent name** even when harness/provider/model changed. Should group by `(agent, harness, provider, model)` and show as separate rows.
- **`session_stats.model`** is populated from the `_stats` chunk (reported by CLI). May differ from `agents.model` if the CLI auto-selects a model.
