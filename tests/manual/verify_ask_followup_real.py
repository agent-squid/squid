#!/usr/bin/env python3
"""Real-CLI verification for the mid-turn ask_followup_question flow.

This is NOT a pytest test — it launches the *real* `claude` CLI, spends tokens,
and depends on the model actually choosing to call ask_followup_question, so it
is nondeterministic. Run it by hand to confirm the fix works end-to-end:

    python3 tests/manual/verify_ask_followup_real.py

What it checks (the load-bearing assumption the unit tests can't cover):
  1. Turn 1 forces an ask_followup_question; Squid soft-completes and surfaces
     the question text, keeping the process alive with a stored pending_followup.
  2. Turn 2 reuses the same live process and delivers the answer via
     parent_tool_use_id. If the real CLI accepts that as the tool result, the
     model continues *using the answer* instead of re-asking the question.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent import runners
from agent.config import CLAUDE_PATH
from agent.runners import run_claude_interactive_cli, _claude_interactive_sessions

TOPIC = "verify-followup"
AGENT = "claude"
# NOTE: ask_followup_question is NOT a base Claude Code tool (that is
# AskUserQuestion). It must be provided by the cwd's project/MCP config. Point
# this at a cwd where the tool is actually available, e.g.:
#   SQUID_VERIFY_CWD=/path/to/project python3 tests/manual/verify_ask_followup_real.py
CWD = os.environ.get("SQUID_VERIFY_CWD", "/tmp")
KEY = ("claude", TOPIC, AGENT, CWD, None, ())

# Explicit enough that a well-behaved model calls the tool before doing anything.
TURN1 = (
    "Before doing anything else, call the ask_followup_question tool to ask me "
    "which color I prefer, with exactly two options: 'red' and 'blue'. "
    "Do not guess — you must ask."
)
ANSWER = "blue"


def _texts(chunks):
    return [c for c in chunks if isinstance(c, str)]


async def _collect(prompt, **kw):
    return [
        c
        async for c in run_claude_interactive_cli(
            prompt, cwd=CWD, topic=TOPIC, agent=AGENT,
            interactive_idle_timeout_s=3600, **kw,
        )
    ]


async def main() -> int:
    if not CLAUDE_PATH:
        print("SKIP: claude CLI not found on PATH")
        return 2
    _claude_interactive_sessions.pop(KEY, None)

    print(f"→ Turn 1 (forcing ask_followup_question), timeout={runners._ASK_FOLLOWUP_RESULT_WAIT}s window")
    turn1 = await _collect(TURN1)
    print("  turn1 text chunks:")
    for t in _texts(turn1):
        print("   ", repr(t[:200]))

    session = _claude_interactive_sessions.get(KEY)
    pending = session.pending_followup if session else None
    alive = bool(session and session.proc and session.proc.returncode is None)
    print(f"  pending_followup={pending}  process_alive={alive}")

    if not pending:
        print("INCONCLUSIVE: model did not emit a blocking ask_followup_question "
              "(auto-handled or answered inline). Re-run; consider a stronger prompt.")
        if session:
            await session.close()
        return 3

    print(f"→ Turn 2 (answering '{ANSWER}' via parent_tool_use_id={pending.get('tool_use_id')})")
    turn2 = await _collect(ANSWER)
    joined = " ".join(_texts(turn2)).lower()
    print("  turn2 text chunks:")
    for t in _texts(turn2):
        print("   ", repr(t[:200]))

    session = _claude_interactive_sessions.get(KEY)
    if session:
        await session.close()

    reacked = ANSWER in joined
    reasked = ("which color" in joined or "red" in joined and "blue" in joined and "prefer" in joined)
    print(f"\n  answer acknowledged={reacked}  looks_like_reask={reasked}")

    if reacked and not reasked:
        print("PASS: real CLI accepted the answer via parent_tool_use_id.")
        return 0
    print("FAIL: model did not clearly consume the answer — inspect turn2 above. "
          "parent_tool_use_id delivery may not satisfy the pending tool_use.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
