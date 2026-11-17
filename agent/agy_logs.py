"""Squid-owned AGY transcripts. Native stdout is preserved verbatim per line."""
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re

log = logging.getLogger(__name__)


def session_log_path(agent: str, session_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", session_id):
        raise ValueError("invalid AGY conversation ID")
    root = Path(os.environ.get("SQUID_LOG_DIR") or Path.home() / ".squid" / "logs")
    owner = hashlib.sha256(agent.encode()).hexdigest()
    return root / "agy" / owner / f"{session_id}.jsonl"


class Capture:
    def __init__(self, agent: str, session_id: str, prompt: str, msg_id):
        self.agent = agent
        self.session_id = session_id
        self.disabled = False
        self.pending = []
        self.pending_size = 0
        self.write({"event": "squid_prompt", "message": {"content": prompt}, "msg_id": msg_id})

    def write(self, record):
        if self.disabled:
            return
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(record, ensure_ascii=False) + "\n"
        self.pending.append(line)
        self.pending_size += len(line)
        if not self.session_id:
            # A valid AGY stream identifies its conversation in init. Bound
            # buffering if a broken CLI emits indefinitely without an ID.
            if self.pending_size > 8 * 1024 * 1024:
                self.disabled = True
                self.pending.clear()
                log.warning("AGY transcript capture exceeded pre-init buffer")
            return
        try:
            path = session_log_path(self.agent, self.session_id)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Append and close on each event: interrupted turns remain viewable.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write("".join(self.pending))
            self.pending.clear()
            self.pending_size = 0
        except (OSError, ValueError):
            self.disabled = True
            self.pending.clear()
            log.warning("Could not capture AGY transcript", exc_info=True)

    def record(self, raw: str):
        if self.disabled:
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict):
            payload = event.get("step_update") or event.get("result") or {}
            candidate = event.get("conversation_id") or (payload.get("conversation_id") if isinstance(payload, dict) else None)
            if isinstance(candidate, str) and candidate:
                if self.session_id and candidate != self.session_id:
                    # Never split a turn or mix another conversation into it.
                    self.disabled = True
                    self.pending.clear()
                    log.warning("AGY conversation ID changed during transcript capture")
                    return
                self.session_id = candidate
        self.write({"event": "squid_agy_stdout", "raw": raw})
