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

**Use your $20 plan like a $100 plan.**

Named sessions, parallel prompts, and goldfish mode — auto lookback or manual context selection — across Claude Code, Codex, Cursor Agent, and DeepSeek via Claude Code.

Most developers burn tokens they never meant to spend. Long sessions drift — stale context, forgotten decisions, dead branches still in the window. The model reads all of it. You pay for all of it.

Squid gives you the controls to stop that.

```text
#launch@claude write the release notes
#launch@codex! review the diff for regressions
#bug@cursor reproduce the auth failure
```

## Core Values

**Every token should earn its place.**
Every prompt shows backend-reported input, output, cache reads, cache writes, reasoning tokens, cost, and latency, plus an observed quota signal when available. Quota deltas are backend-wide estimates rather than exact per-prompt attribution, especially during parallel work. You see when a long session is helping and when it's dead weight. You decide, every time.

**Goldfish when you need fresh eyes.**
`!` runs a clean prompt. `!3` sends only the last three exchanges. You choose the lookback window — or cut it to zero. Stale context is a cost you can eliminate.

**Sessions that outlive your terminal.**
Close the tab, reboot the machine, switch to your phone. Named sessions survive all of it. Your work is `#topic@agent`, not a terminal PID nobody can find tomorrow.

**Parallel without chaos.**
Run Claude and Codex on the same problem at the same time. Compare the answers. The sessions don't interfere — each gets its own lane, its own queue, its own context.

**Context you curate, not context that accumulates.**
Pin a good answer from one agent and inject it into another. Clear a session when it's drifted too far. Add a `CLAUDE.md` scoped to a specific topic. You are the editor of what each agent knows.

**Your machine. Your credentials. Any screen.**
The agent runs where your repo lives. Squid gives that machine a browser interface — including your phone on the couch. Nothing moves to the cloud.

## What You Get

- **Named agent lanes:** `#topic@agent` gives every workstream a durable identity.
- **Dynamic tags:** create topics as you type, reuse sticky agents, filter by tag, and delete stale topics when the work is done.
- **Native resumable sessions:** Squid tracks session handles while the CLI owns its real context.
- **Parallel work:** different topics and agents run independently.
- **Adhoc mode:** `#topic@agent!` runs a fresh one-off job immediately without polluting the main session.
- **Session vs. limited-context comparison:** compare a fully resumed lane against an adhoc prompt that includes only the last N exchanges.
- **Live progress bubble:** watch queued state, tool/status output, and partial response progress while the CLI is working.
- **Context pins:** pin a useful answer and inject it into another session or adhoc turn.
- **Process controls:** stop one process from the UI, stop by command, stop a topic, drain queues, clear sessions, and compact/reset context.
- **History and filtering:** scan past work by topic, agent, or adhoc lane.
- **Analytics:** review usage by time, topic, or agent, plus live process state.
- **Per-prompt usage:** every completed prompt can show input, output, cache, reasoning, cost, duration, and an observed backend-wide quota signal when the backend exposes them.
- **Phone/tablet access:** lie on the couch while your local machine keeps coding.

## Setup

### 1. Install at least one agent CLI

Squid needs at least one coding agent to talk to. Install whichever you use:

```bash
curl -fsSL https://claude.ai/install.sh | bash       # Claude Code
curl -fsSL https://chatgpt.com/codex/install.sh | sh # OpenAI Codex
curl -fsS https://cursor.com/install | bash          # Cursor Agent
curl -fsSL https://opencode.ai/install | bash        # OpenCode
```

DeepSeek uses the Claude Code CLI, so no additional CLI is required beyond `claude`.

### 2. Install squid

```bash
bin/install.sh   # sets up Python venv; checks for agent CLIs
bin/start.sh     # starts the server; bootstraps ~/.squid/ on first run
```

`install.sh` creates a `.venv` in the install directory and checks which agent CLIs are on your PATH.

`start.sh` bootstraps `~/.squid/` on first run — config, database, context files, and logs all live there and survive tarball updates.

### 3. Configure (optional)

Edit `~/.squid/squid.yaml` directly or use the YAML editor in the Agents view to
change backends, port, `localfile_roots`, or timeouts. Most changes apply after:

```bash
bin/start.sh --restart
```

To use the configured `deepcla` backend, export your DeepSeek API key before starting Squid:

```bash
export DEEPSEEK_API_KEY="<your-deepseek-api-key>"
```

The shipped YAML example defines `deepcla` with `driver: claude`, canonical
DeepSeek connection fields, and `gauge: deepseek`. Create an agent using backend
`deepcla` and model `deepseek-v4-pro`, then use it normally:

```text
#work@deepcla implement the feature
```

To stop without restarting: `bin/stop.sh`

**Local access:** `http://127.0.0.1:<port>` (default 8000, set in `~/.squid/squid.yaml`)
**Remote access (Tailscale):** type `/remote` in the chat — it returns a QR code with the full HTTPS URL. Point your phone camera at it to open squid in one tap.

> **Local vs. remote URLs**
> Always use `http://127.0.0.1:8000` in the browser **on the machine running squid**.
> The Tailscale HTTPS URL (`https://<machine>.ts.net/`) is for **other devices only** — phone, tablet, another laptop.
> It does not work in the browser on the same machine where squid is running.

## Backends

| Backend       | CLI                                            | Install                                                   | Sessions  |
|---------------|------------------------------------------------|-----------------------------------------------------------|-----------|
| `claude`      | `claude` (Claude Code)                         | `curl -fsSL https://claude.ai/install.sh \| bash`         | resumable |
| `deepcla`     | `claude` + DeepSeek Anthropic-compatible API | Claude Code CLI + DeepSeek API key                         | resumable |
| `codex`       | `codex` (OpenAI Codex)                         | `curl -fsSL https://chatgpt.com/codex/install.sh \| sh`   | resumable |
| `cursor`      | `cursor-agent` (Cursor)                        | `curl -fsS https://cursor.com/install \| bash`            | resumable |
| `opencode`    | `opencode`                                     | `curl -fsSL https://opencode.ai/install \| bash`          | resumable |

A **driver** is Squid's coded coding-agent integration (`claude`, `codex`, `cursor`, or `opencode`). A **backend** is a YAML-configured instance of one driver: endpoint, credentials, arguments, gauge, UI color, default model, and protocol. Multiple backends can share a driver; for example `deepcla` uses the Claude driver with a DeepSeek endpoint. See `config/squid.yaml.example`.

Drivers communicate through protocols. Today the primary protocol is `oneshot-cli`: Squid starts a CLI process for one turn, parses its output, then lets it exit. Squid's model also supports long-lived protocols: `interactive-cli` when a CLI exposes a structured stdin/stdout interface, and `interactive-pty` when the CLI's real interactive behavior only exists through a terminal. Interactive backends may keep a warm CLI process between turns, then stop it after the backend's idle timeout while preserving the native session for resume.

## Input syntax

```
#topic@agent  message    — routed to agent session, FIFO queued
#topic@agent! message    — adhoc: parallel, no queue, not in session context
#topic@agent!3 message   — adhoc with only the last 3 exchanges as context
#topic        message    — uses sticky agent (last used on this topic)
#topic!       message    — adhoc with sticky agent
```

**`#topic`** is sticky — it remembers the last agent used on that topic. Sending `#work@opus message` updates the sticky, so bare `#work message` continues in the same session.

**`!` adhoc mode** runs the prompt immediately in parallel using the oneshot approach (selected context injected as text). The result is saved in history but excluded from the session's context unless explicitly pinned. This lets you compare responses between the resumable session and a fresh oneshot call.

This makes comparison easy. Ask the fully resumed session with `#topic@agent`, then ask a limited-context version with `#topic@agent!3`, `#topic@agent!1`, or `#topic@agent!`. Each response shows its own token stats, so you can see whether the long session is helping or whether stale context is hurting.

## Agents

An agent defines an identity: `(backend, model, cwd)`. The backend supplies the driver and defaults; the agent may override the model and working directory. These three are **locked at session creation** and cannot change for the lifetime of that session. To use a different configuration on the same topic, create a new agent — it gets its own parallel queue lane.

Agents are **immutable** — they cannot be edited after creation. If you need a different config, delete and recreate with a new name. (Deleting an agent does not affect existing messages in history.)

Create an agent via the UI settings panel, or directly:

```bash
curl -X POST http://127.0.0.1:8000/config/agents \
  -H 'Content-Type: application/json' \
  -d '{"name": "opus", "backend": "claude", "model": "claude-opus-4-7", "cwd": "/tmp/<user>/squid/work"}'
```

| Field     | Type    | Description                                                                      |
|-----------|---------|----------------------------------------------------------------------------------|
| `name`    | string  | Agent identifier — used in `#topic@agent` syntax                                 |
| `backend` | string  | Backend ID configured under `backends` in `~/.squid/squid.yaml`                |
| `model`   | string  | Model name passed as `--model` to the CLI (optional)                             |
| `cwd`     | string  | Absolute path for the subprocess cwd; `null` = `/tmp/<user>/squid`              |
| `timeout` | integer | Response timeout in seconds; overrides the global 1800 s default                 |

### Agent required

`@agent` must resolve to a known agent. If it doesn't exist, the UI prompts you to create it inline before the message is sent. There is no auto-detection of backend from model name — the agent locks all three dimensions `(backend, model, cwd)` explicitly.

`#topic@agent` is a route. The topic owns the conversation/history; the agent owns execution config.

### Context directory and bare-run design

`start.sh` creates `/tmp/<user>/squid` (where `<user>` is the OS username running squid) and keeps it in sync with `<repo>/context/` via rsync. All CLI subprocesses run from `/tmp/<user>/squid` by default.

**Why `/tmp/<user>/squid` and not the repo directory:** Claude Code scans the working directory and all parent directories for `CLAUDE.md` files. Running from anywhere under `~/` would load `~/CLAUDE.md` if it exists — injecting unintended personas, MCP tool connections, or agent config. `/tmp/` has no `CLAUDE.md` in its path, so subprocesses start clean. The per-user subdirectory avoids permission conflicts when multiple OS users run squid on the same machine.

**Adding context for an agent:** create a subdirectory under `context/` and put a `CLAUDE.md` there. Then set the agent `cwd` to `/tmp/<user>/squid/<subdir>`.

```
context/
  work/
    CLAUDE.md    ← loaded when agent cwd = /tmp/<user>/squid/work
  coding/
    CLAUDE.md    ← loaded when agent cwd = /tmp/<user>/squid/coding
```

## Sessions

Each `(topic, agent)` pair maintains a resumable CLI session. The `cwd` used at session creation is locked for the session's lifetime — this is required because the CLI stores session files keyed to the working directory.

Clear a session to start fresh (picks up current agent config):

```bash
curl -X DELETE 'http://127.0.0.1:8000/topics/work/session?agent=opus'
```

Inspect what a session has seen and what's pending injection:

```bash
curl 'http://127.0.0.1:8000/context/work?agent=opus'
```

```json
{
  "session_id": "abc-123",
  "cwd": "/tmp/<user>/squid/work",
  "pending_injections": [{ "id": 42, "role": "assistant", "content": "…", "source_agent": "sonnet" }],
  "already_injected": [{ "id": 38, "injected_at": "2026-05-25T10:00:00Z" }]
}
```

## Context curation

**Pinning** is the cross-session sharing primitive. Pinning a message (from any session, adhoc turn, or other topic) queues it for one-time injection into any `(topic, agent)` session that hasn't absorbed it yet. The first message after a pin absorbs it; subsequent messages don't re-inject it.

This means you curate context once — pin a useful response from one model, and every other model on that topic automatically picks it up on its next turn.

Clearing a session also clears its injection log — a fresh session re-absorbs all currently-pinned messages on its first message.

## Security

Squid has two security layers, both configured in `~/.squid/squid.yaml`.

**Layer 1 — Host binding** (enforced at startup): `server.host` must be
`127.0.0.1`. Public IPs, LAN IPs, and `0.0.0.0` are blocked — the server
refuses to start. Remote access is handled exclusively by `tailscale serve`,
which provides its own device-level authentication.

**Layer 2 — `/localfile` path allowlist**: Squid's own `~/.squid` state directory
is visible to the single-user web UI. Other local files are restricted to explicit
directories:
```yaml
server:
  localfile_roots:
    - "/tmp/<user>/squid"
```
Replace `<user>` with your OS username. Add other directories in YAML or use the
file viewer's **Allow directory** action. The selected directory applies immediately;
other configuration changes require a restart.

## Couch Coding With Tailscale

Squid is most useful when your local machine can keep working while you are away from the desk.

Tailscale is a good fit for this. Its Personal plan is free for non-commercial personal use, and it creates a private WireGuard-based network across your own devices. Your phone, tablet, laptop, Mac mini, and workstation can talk inside the tailnet without opening a public port.

Squid always binds to `127.0.0.1` — it never touches a network interface directly.
`bin/start.sh` automatically configures `tailscale serve` if Tailscale is installed,
so remote access is set up on first start with no extra steps.

If Tailscale is not installed or not logged in, squid still starts and is accessible
locally. Run the following manually when ready:

```bash
tailscale serve --bg 127.0.0.1:8000
```

Type `/remote` in the chat to get a QR code with the full URL:

```
https://<machine-name>.<tailnet>.ts.net/
```

Tailscale auto-provisions a TLS cert for the full domain — browsers show the
padlock. The full domain is required; the short hostname alone
(`https://<machine-name>/`) does not work because the cert is scoped to
`*.ts.net` and browsers enforce an exact match.

Rename your machine in Tailscale admin for a clean URL (e.g. `agent-squid`).

## How Squid Is Different

Squid is not another general AI chat app. Open WebUI and LibreChat are broad self-hosted chat platforms for many providers, RAG, plugins, users, memory, and web search.

Squid is narrower: it controls real local coding-agent CLIs and preserves their session behavior.

Most chat UIs send messages to a model API and render text back. Even when they support tools or agents, the chat app is usually the runtime. Squid is different: the runtime is still the local CLI agent. Claude Code, Codex, or Cursor Agent is the process doing the work on your machine. Squid is the interactive control layer around those processes.

That difference matters:

- **Real CLI sessions, not copied chat history:** Squid resumes the agent's native session instead of pretending to be the agent with a replayed transcript.
- **Working-directory awareness:** sessions are tied to the cwd where the CLI actually runs, which matters for local project context and resume behavior.
- **Process ownership:** Squid can show live processes, kill the exact running job, drain queued jobs, and recover after disconnects.
- **Topic and agent lanes:** `#topic@agent` is closer to a named terminal workspace than a chat room.
- **Progress while work happens:** the thought bubble surfaces status, queued state, tool activity, and partial output before the final answer lands.
- **UI plus command control:** click to stop a specific run, or type commands like `/stop`, `/stopall`, `/clear`, `/compact`, and `/filter`.
- **Analytics attached to real work:** token usage is tied to each prompt and can be rolled up by topic, agent, or time.
- **Built-in context experiments:** compare a native resumed session against an adhoc turn with only selected recent context (side-by-side quota comparison: TBD).
- **Local machine as the backend:** your Mac mini or workstation stays the execution environment, so the agent has the same filesystem, shell, credentials, and installed tools it would have in a terminal.

| Category | Examples | Squid's difference |
|---|---|---|
| Self-hosted AI chat | Open WebUI, LibreChat | Squid runs local CLI coding agents instead of replacing them with a provider chat UI. |
| Single-agent CLIs | Claude Code, Codex CLI, Cursor Agent | Squid gives them shared browser/mobile UI, topics, queues, history, controls, and analytics. |
| IDE agents | Cursor, Cline, VS Code Copilot | Squid is editor-agnostic and works even when the IDE is not open. |
| Terminal pair programmers | Aider, OpenCode | Squid is an orchestration layer, not a coding engine. |

Use the agent directly when one terminal is enough. Use Squid when you want several local agents, named sessions, mobile access, process control, recoverable long-running work, and analytics that explain what each lane is costing you.

## Endpoints

### `POST /chat`

| Field      | Type           | Default     | Description                                      |
|------------|----------------|-------------|--------------------------------------------------|
| `message`  | string         | required    | Prompt text                                      |
| `topic`    | string         | `"default"` | Conversation thread identifier                   |
| `agent`    | string         | `null`      | Agent to use; must exist or returns 400          |
| `lookback` | int \| `"all"` | `5`         | History exchanges to inject (adhoc one-shot only) |
| `adhoc`    | bool           | `false`     | Run as a parallel one-shot turn outside session queue |

```bash
curl -N -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "summarise this", "topic": "work", "agent": "opus"}'
```

#### SSE event types

| Event    | Payload                            | Meaning                                      |
|----------|------------------------------------|----------------------------------------------|
| *(none)* | streamed text chunk                | Response content                             |
| `meta`   | `{agent, backend, msg_id, adhoc}`  | Sent first; identifies the response          |
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

- `stop` — SIGTERM the running process for a topic (all agent lanes), leaves queue intact
- `stopall` — SIGTERM + drain queue for all agent lanes of a topic
- `deq` — remove a pending item from the queue without killing the running one
- `list` — return all topics with last prompt and metadata

### `GET /history`

Query params: `topic=<name>`, `agent=<name>`, `offset=0`, `limit=5`

Filters by topic and/or agent. Used by the UI's clickable tag filter.

### `GET /topics`

Returns all topics with queue depth and active-process status across all agent lanes.

### `GET /context/{topic}?agent=<name>`

Returns the session state for a `(topic, agent)` pair: session ID, locked cwd, messages pending injection, and already-injected message IDs.

### `GET /topics/{topic}/session?agent=<name>`

Same as `/context/{topic}` — alias for session inspection.

### `DELETE /topics/{topic}/session?agent=<name>`

Clears the stored session ID, cwd, and injection log for this `(topic, agent)`. The next message starts a fresh session using the current agent config.

### `GET /health`

```json
{
  "status": "ok",
  "backends": {
    "claude":  { "driver": "claude", "protocol": "oneshot-cli", "available": true,  "path": "/usr/local/bin/claude", "gauge": { "type": "claude" }, "gauge_authed": true  },
    "codex":   { "driver": "codex",  "protocol": "oneshot-cli", "available": true,  "path": "/usr/local/bin/codex",  "gauge_authed": false },
    "cursor":  { "driver": "cursor", "protocol": "oneshot-cli", "available": false, "path": null,                    "gauge_authed": false }
  }
}
```

### `GET /stats`

Query params: `period=daily|hourly`, `group=time|topic|model`

### `GET/POST/DELETE /config/agents`

Manage named agent configs. Agents cannot be edited after creation — delete and recreate to change config.

### `GET /processes`

Lists currently running CLI subprocesses with backend, topic, agent, and elapsed time.

## In-chat commands

Type these directly in the message box (no `#topic` prefix needed):

| Command    | Effect                                                        |
|------------|---------------------------------------------------------------|
| `/stop`    | Kill running process for the current topic (all agent lanes)  |
| `/stopall` | Kill + drain queue for the current topic                      |
| `/clear`   | Clear the current session                                     |
| `/compact` | Compact or reset context (auto and cross-agent compaction: TBD) |
| `/f`, `/filter` | Filter history; scopes use `#topic`, `@agent`, `@agent!`, or `@agent*` |
| `/remote`  | Show QR code with full HTTPS URL for mobile access            |
| `deq`      | Drain entire pending queue                                    |
| `deq N`    | Remove Nth queued item (1=first, -1=last)                     |

Topic browsing and deletion are handled by typing `#` in the input box — the autocomplete popup lists all topics with a `✕` button to delete each one's sessions.

## Good Fit

Squid is a good fit if:

- You run multiple coding-agent terminals at once.
- You want to recover and operate sessions by name.
- You use a local Mac mini, workstation, or always-on machine for agent work.
- You want phone/tablet control without moving execution to the cloud.
- You compare or combine several agent CLIs.
- You want to compare full-session context against limited-history prompts.
- You care about token usage, cost, cache behavior, and per-topic or per-agent analytics.

It may be overkill if:

- You only ever use one agent in one terminal.
- You want a hosted team chat product.
- You want Squid to be the coding agent itself.
