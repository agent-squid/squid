# chat-server

Minimal FastAPI server that relays chat messages to `claude` or `codex` CLI
and streams the response back over SSE.

## Setup

```bash
pip install -r requirements.txt
```

Both CLIs are optional — install whichever you use:

```bash
npm install -g @anthropic-ai/claude-code   # claude CLI
npm install -g @openai/codex               # codex CLI
```

## Run

```bash
uvicorn server:app --port 8000
```

Or with auto-reload during development:

```bash
uvicorn server:app --port 8000 --reload
```

## Endpoints

### `GET /health`

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "backends": {
    "claude": { "available": true,  "path": "/usr/local/bin/claude" },
    "codex":  { "available": false, "path": null }
  }
}
```

### `POST /chat`

| Field     | Type   | Default  | Description                        |
|-----------|--------|----------|------------------------------------|
| `message` | string | required | Prompt text                        |
| `backend` | string | `"auto"` | `"auto"` \| `"claude"` \| `"codex"` |
| `cwd`     | string | `null`   | Working directory for the CLI      |

**Streaming with curl:**

```bash
curl -N -X POST http://localhost:8000/chat \
     -H 'Content-Type: application/json' \
     -d '{"message": "write a hello world in Python"}'
```

**Force a specific backend:**

```bash
curl -N -X POST http://localhost:8000/chat \
     -H 'Content-Type: application/json' \
     -d '{"message": "explain this code", "backend": "codex"}'
```

**With a working directory (for code-aware context):**

```bash
curl -N -X POST http://localhost:8000/chat \
     -H 'Content-Type: application/json' \
     -d '{"message": "what does this repo do?", "cwd": "/path/to/project"}'
```

### SSE event types

| Event        | Meaning                          |
|--------------|----------------------------------|
| *(default)*  | `data:` lines — streamed content |
| `done`       | Stream finished cleanly          |
| `error`      | CLI not found or process error   |

## Built-in REPL

With the server already running, open a second terminal:

```bash
python server.py
```

```
you> write fizzbuzz in Go
ai>  func main() { ...
you> codex:refactor this to use generics
ai>  ...
```

Prefix with `claude:` or `codex:` to pick a backend per message.
