"""
server.py — Chat relay server.

Endpoints
---------
POST /chat
    Body (JSON): { "message": "...", "backend": "auto|claude|codex", "cwd": "/optional/path" }
    Response: text/event-stream  (SSE)

    SSE event format:
        data: <text chunk>          — streamed content
        event: done                 — stream finished cleanly
        event: error\ndata: <msg>  — something went wrong

GET /health
    Returns JSON with which CLIs are available.

Usage
-----
    uvicorn agent.server:app --port 8000

Then from a terminal:
    curl -N -X POST http://localhost:8000/chat \
         -H 'Content-Type: application/json' \
         -d '{"message": "write a hello world in Python"}'
"""

import asyncio
import json
import logging
from typing import AsyncGenerator, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

from .config import CLAUDE_PATH, CODEX_PATH
from .runners import run_auto, run_claude, run_codex, CLINotFoundError, CLIError

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

def _claude_logged_in() -> bool:
    """Returns True if claude reports a logged-in session."""
    if not CLAUDE_PATH:
        return False
    try:
        import subprocess
        result = subprocess.run(
            [CLAUDE_PATH, "auth", "status"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn"))
    except Exception:
        return False


def _check_deps():
    import os as _os
    missing, warnings = [], []

    if not CLAUDE_PATH:
        missing.append("claude  →  npm install -g @anthropic-ai/claude-code")
    elif not _claude_logged_in():
        warnings.append("claude is installed but not logged in  →  run: claude login")

    if not CODEX_PATH:
        missing.append("codex   →  npm install -g @openai/codex")
    elif not _os.environ.get("OPENAI_API_KEY"):
        warnings.append("codex is installed but OPENAI_API_KEY is not set")

    if missing:
        log.warning("Missing CLI tools (run ./install.sh):\n  " + "\n  ".join(missing))
    if warnings:
        log.warning("Auth issues:\n  " + "\n  ".join(warnings))
    if not missing and not warnings:
        log.info("claude=%s  codex=%s", CLAUDE_PATH, CODEX_PATH)

_check_deps()

app = FastAPI(title="Squid", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UI_DIR = Path(__file__).parent.parent / "ui"

# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    backend: Literal["auto", "claude", "codex"] = Field("auto")
    cwd: Optional[str] = Field(None)

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def sse_chunk(data: str) -> str:
    escaped = data.replace("\n", "\ndata:")
    return f"data:{escaped}\n\n"

def sse_event(event: str, data: str = "") -> str:
    lines = f"event: {event}\n"
    if data:
        lines += f"data: {data}\n"
    return lines + "\n"

async def stream_response(
    message: str,
    backend: str,
    cwd: Optional[str],
) -> AsyncGenerator[str, None]:
    runner = {"auto": run_auto, "claude": run_claude, "codex": run_codex}[backend]
    try:
        async for chunk in runner(message, cwd=cwd):
            if isinstance(chunk, dict) and "_stats" in chunk:
                yield sse_event("stats", json.dumps(chunk["_stats"]))
            else:
                yield sse_chunk(chunk)
            await asyncio.sleep(0)
        yield sse_event("done")
    except CLINotFoundError as exc:
        log.warning("CLI not found: %s", exc)
        yield sse_event("error", str(exc))
    except CLIError as exc:
        log.error("CLI error: %s", exc)
        yield sse_event("error", str(exc))
    except Exception as exc:
        log.exception("Unexpected error")
        yield sse_event("error", f"Internal server error: {exc}")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest):
    log.info("chat  backend=%s  cwd=%s  msg=%.80r", req.backend, req.cwd, req.message)
    return StreamingResponse(
        stream_response(req.message, req.backend, req.cwd),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "backends": {
            "claude": {"available": bool(CLAUDE_PATH), "path": CLAUDE_PATH},
            "codex":  {"available": bool(CODEX_PATH),  "path": CODEX_PATH},
        },
    })


# Mount UI static files last so API routes take precedence
if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
