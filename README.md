# 🦑 Agent-Squid
```
                   🦑 AGENT
    ██████╗ ██████╗ ██╗   ██╗██╗██████╗
   ██╔════╝██╔═══██╗██║   ██║██║██╔══██╗
   ╚█████╗ ██║   ██║██║   ██║██║██║  ██║
    ╚═══██╗██║▄▄ ██║██║   ██║██║██║  ██║
   ██████╔╝╚██████╔╝╚██████╔╝██║██████╔╝
   ╚═════╝  ╚══▀▀═╝  ╚═════╝ ╚═╝╚═════╝
```

**Agent-Squid** is an open-source web chat for local CLI-native AI agents.
Via Tailscale, access your local CLI agents from anywhere safely — phone, tablet, any device on your network.

* `#topic@agent` — named sessions with FIFO queuing per topic and agent
* `#topic@agent!` — adhoc flag: run in parallel, step-independent, not in session context
* Different agents, different topics run in parallel automatically
* Resumable sessions — the CLI owns its context; resume it after a week like it happened just before.
* Curate context across models — setup auto lookback or pin any responses, it gets injected into any session that hasn't seen it yet
* Full visibility into what each session sees and what it costs

## Setup

```bash
bash install.sh   # installs Python venv + checks for claude/codex/copilot CLIs
bash start.sh     # starts the server on http://127.0.0.1:8000
```

The server auto-runs `install.sh` on first launch if the venv is missing.
Set `HOST=0.0.0.0` to listen on all interfaces, `PORT=8899` to change the port.

## Backends

| Backend       | CLI                                              | Install                                        | Sessions      |
|---------------|--------------------------------------------------|------------------------------------------------|---------------|
| `claude`      | `claude` (Claude Code)                           | `npm install -g @anthropic-ai/claude-code`     | resumable     |
| `codex`       | `codex` (OpenAI Codex)                           | `npm install -g @openai/codex`                 | resumable     |
| `cursor`      | `cursor-agent` (Cursor)                          | install from cursor.com                        | resumable     |
| `copilot`     | `copilot` (GitHub Copilot)                       | `gh extension install github/gh-copilot`       | resumable     |
| `antigravity` | `agy` (Google Antigravity)                       | install from https://antigravity.google        | resumable     |
| `auto`        | tries claude → cursor → agy → codex → copilot | —                                              | —             |

## Input syntax

```
#topic@alias  message    — routed to alias session, FIFO queued
#topic@alias! message    — adhoc: parallel, no queue, not in session context
#topic        message    — uses sticky alias (last used on this topic)
#topic!       message    — adhoc with sticky alias
```

**`#topic`** is sticky — it remembers the last alias used on that topic. Sending `#work@opus message` updates the sticky, so bare `#work message` continues in the same session.

**`!` adhoc mode** runs the prompt immediately in parallel using the oneshot approach (selected context injected as text). The result is saved in history but excluded from the session's context unless explicitly pinned. This lets you compare responses between the resumable session and a fresh oneshot call.

## Aliases

An alias defines an agent identity: `(backend, model, cwd)`. These three are **locked at session creation** and cannot change for the lifetime of that session. To use a different configuration on the same topic, create a new alias — it gets its own parallel queue lane.

Aliases are **immutable** — they cannot be edited after creation. If you need a different config, delete and recreate with a new name. (Deleting an alias does not affect existing messages in history.)

Create an alias via the UI settings panel, or directly:

```bash
curl -X POST http://localhost:8000/config/aliases \
  -H 'Content-Type: application/json' \
  -d '{"name": "opus", "backend": "claude", "model": "claude-opus-4-7", "cwd": "/tmp/squid/work"}'
```

| Field     | Type    | Description                                                                      |
|-----------|---------|----------------------------------------------------------------------------------|
| `name`    | string  | Alias identifier — used in `#topic@alias` syntax                                 |
| `backend` | string  | `auto` \| `claude` \| `cursor` \| `antigravity` \| `codex` \| `copilot` |
| `model`   | string  | Model name passed as `--model` to the CLI (optional)                             |
| `cwd`     | string  | Absolute path for the subprocess cwd; `null` = `/tmp/squid`                     |
| `timeout` | integer | Response timeout in seconds; overrides the global 1800 s default                 |

### Alias required

`@alias` must resolve to a known alias. If it doesn't exist, the UI prompts you to create it inline before the message is sent. There is no auto-detection of backend from model name — the alias locks all three dimensions `(backend, model, cwd)` explicitly.

### Context directory and bare-run design

`start.sh` creates a symlink `/tmp/squid` → `<repo>/context/` on every launch. All CLI subprocesses run from `/tmp/squid` by default.

**Why `/tmp/squid` and not the repo directory:** Claude Code scans the working directory and all parent directories for `CLAUDE.md` files. Running from anywhere under `~/` would load `~/CLAUDE.md` if it exists — injecting unintended personas, MCP tool connections, or agent config. `/tmp/` has no `CLAUDE.md` in its path, so subprocesses start clean.

**Adding context for an alias:** create a subdirectory under `context/` and put a `CLAUDE.md` there. Then set the alias `cwd` to `/tmp/squid/<subdir>`.

```
context/
  work/
    CLAUDE.md    ← loaded when alias cwd = /tmp/squid/work
  coding/
    CLAUDE.md    ← loaded when alias cwd = /tmp/squid/coding
```

## Sessions

Each `(topic, alias)` pair maintains a resumable CLI session. The `cwd` used at session creation is locked for the session's lifetime — this is required because the CLI stores session files keyed to the working directory.

Clear a session to start fresh (picks up current alias config):

```bash
curl -X DELETE 'http://localhost:8000/topics/work/session?alias=opus'
```

Inspect what a session has seen and what's pending injection:

```bash
curl 'http://localhost:8000/context/work?alias=opus'
```

```json
{
  "session_id": "abc-123",
  "cwd": "/tmp/squid/work",
  "pending_injections": [{ "id": 42, "role": "assistant", "content": "…", "source_alias": "sonnet" }],
  "already_injected": [{ "id": 38, "injected_at": "2026-05-25T10:00:00Z" }]
}
```

## Context curation

**Pinning** is the cross-session sharing primitive. Pinning a message (from any session, adhoc turn, or other topic) queues it for one-time injection into any `(topic, alias)` session that hasn't absorbed it yet. The first message after a pin absorbs it; subsequent messages don't re-inject it.

This means you curate context once — pin a useful response from one model, and every other model on that topic automatically picks it up on its next turn.

Clearing a session also clears its injection log — a fresh session re-absorbs all currently-pinned messages on its first message.

## Endpoints

### `POST /chat`

| Field      | Type           | Default     | Description                                      |
|------------|----------------|-------------|--------------------------------------------------|
| `message`  | string         | required    | Prompt text                                      |
| `topic`    | string         | `"default"` | Conversation thread identifier                   |
| `alias`    | string         | `null`      | Alias to use; must exist or returns 400          |
| `lookback` | int \| `"all"` | `5`         | History exchanges to inject (adhoc/oneshot only) |
| `adhoc`    | bool           | `false`     | Run as oneshot, parallel, outside session queue  |

```bash
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "summarise this", "topic": "work", "alias": "opus"}'
```

#### SSE event types

| Event    | Payload                            | Meaning                                      |
|----------|------------------------------------|----------------------------------------------|
| *(none)* | streamed text chunk                | Response content                             |
| `meta`   | `{alias, backend, msg_id, adhoc}`  | Sent first; identifies the response          |
| `status` | partial text                       | Streaming token preview (thinking bubble)    |
| `queued` | `{topic, position}`                | Request is queued behind another             |
| `stats`  | token/cost/latency object          | Usage stats after the response               |
| `done`   | —                                  | Stream finished cleanly                      |
| `error`  | error message string               | CLI error or process failure                 |

### `POST /cmd`

| Field     | Type   | Description                                          |
|-----------|--------|------------------------------------------------------|
| `command` | string | `stop` \| `stopall` \| `deq` \| `list`              |
| `topic`   | string | Target topic (default: `"default"`)                  |
| `pos`     | int    | For `deq`: queue position (1=first, -1=last, …)      |

- `stop` — SIGTERM the running process for a topic (all alias lanes), leaves queue intact
- `stopall` — SIGTERM + drain queue for all alias lanes of a topic
- `deq` — remove a pending item from the queue without killing the running one
- `list` — return all topics with last prompt and metadata

### `GET /history`

Query params: `topic=<name>`, `alias=<name>`, `offset=0`, `limit=5`

Filters by topic and/or alias. Used by the UI's clickable tag filter.

### `GET /topics`

Returns all topics with queue depth and active-process status across all alias lanes.

### `GET /context/{topic}?alias=<name>`

Returns the session state for a `(topic, alias)` pair: session ID, locked cwd, messages pending injection, and already-injected message IDs.

### `GET /topics/{topic}/session?alias=<name>`

Same as `/context/{topic}` — alias for session inspection.

### `DELETE /topics/{topic}/session?alias=<name>`

Clears the stored session ID, cwd, and injection log for this `(topic, alias)`. The next message starts a fresh session using the current alias config.

### `GET /health`

```json
{
  "status": "ok",
  "backends": {
    "claude":       { "available": true,  "path": "/usr/local/bin/claude" },
    "codex":        { "available": true,  "path": "/usr/local/bin/codex"  },
    "copilot":      { "available": false, "path": null }
  }
}
```

### `GET /stats`

Query params: `period=daily|hourly`, `group=time|topic|model`

### `GET/POST/DELETE /config/aliases`

Manage named alias configs. Aliases cannot be edited after creation — delete and recreate to change config.

### `GET /processes`

Lists currently running CLI subprocesses with backend, topic, alias, and elapsed time.

## In-chat commands

Type these directly in the message box (no `#topic` prefix needed):

| Command    | Effect                                                        |
|------------|---------------------------------------------------------------|
| `stop`     | Kill running process for the current topic (all alias lanes)  |
| `stopall`  | Kill + drain queue for the current topic                      |
| `deq`      | Drain entire pending queue                                    |
| `deq N`    | Remove Nth queued item (1=first, -1=last)                     |

Topic browsing and deletion are handled by typing `#` in the input box — the autocomplete popup lists all topics with a `✕` button to delete each one's sessions.
