import asyncio
import datetime
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from .config import SQUID_HOME
from .backends import get_backend
from .runners import run_claude, runner_for_backend
from .stats_db import get_default_agent, get_topic_messages_for_period

log = logging.getLogger(__name__)

_JOURNAL_DIR = Path(__file__).parent.parent / "context" / "journals"
_STATE_FILE = _JOURNAL_DIR / ".state.json"


def _current_week() -> tuple[str, str, str]:
    today = datetime.date.today()
    iso = today.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    monday = today - datetime.timedelta(days=iso[2] - 1)
    week_start = monday.isoformat() + "T00:00:00Z"
    week_end = (monday + datetime.timedelta(days=7)).isoformat() + "T00:00:00Z"
    return week_key, week_start, week_end


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_JOURNAL_DIR, prefix=".state_", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _needs_generation(scope: str, week_key: str, state: dict) -> bool:
    entry = state.get(scope)
    if not entry:
        return True
    if entry.get("week") != week_key:
        return True
    generated_at = entry.get("generated_at", "")
    if generated_at and generated_at[:10] == datetime.date.today().isoformat():
        return False
    return True


def _build_prompt(topic: str, agent: Optional[str], week_key: str,
                  week_start: str, week_end: str, messages: list[dict]) -> str:
    year, w = week_key.split("-W")
    week_num = int(w)
    monday = datetime.date.fromisoformat(week_start[:10])
    sunday = monday + datetime.timedelta(days=6)
    date_range = f"{monday.strftime('%b %-d')}–{sunday.strftime('%-d, %Y')}"

    scope_label = f"#{topic}@{agent}" if agent else f"#{topic}"
    header = f"{scope_label} — Week {week_num}, {year} ({date_range})"

    conv_lines = []
    for msg in messages:
        role = "User" if msg["role"] == "user" else "Assistant"
        conv_lines.append(f"**{role}:** {msg['content'].strip()}")
    conversation = "\n\n".join(conv_lines)

    return f"""You are generating a weekly journal entry for an AI coding assistant conversation log.

Analyze the following conversation from topic {scope_label} and produce a structured Markdown journal entry.
The journal output will be passed directly into a future agent's input as context.
Make it useful for handoff: write concrete facts, name files and commands when known,
separate completed work from unresolved work, and avoid vague narrative.

<conversation>
{conversation}
</conversation>

Produce ONLY the following Markdown, with no preamble:

# {header}

## Summary
2–3 sentences describing what was worked on this week.

## How to use this
Brief instructions for the next agent: what context to trust, what to verify before acting, and which open threads are most relevant if the user resumes this topic.

## File edits
Bullet list of files modified and what changed. Infer from the conversation. If none, write "None noted."

## Key decisions
Bullet list of significant choices and their reasoning. If none, write "None noted."

## Constraints discovered
Bullet list of API limits, gotchas, or things to avoid. If none, write "None noted."

## Open threads
Bullet list of deferred work or unresolved questions. If none, write "None noted."

## Lessons
Bullet list of what failed and why, or what worked better than expected. If none, write "None noted."

---
*Generated {datetime.date.today().isoformat()} by squid*"""


async def _run_generation(prompt: str) -> str:
    agent_cfg = get_default_agent() or {}
    backend_id = agent_cfg.get("backend") or "claude"
    gen_agent = agent_cfg.get("name", "claude")
    model = agent_cfg.get("model")
    backend = get_backend(backend_id)
    runner = runner_for_backend(backend, adhoc=True) if backend else None
    runner = runner or run_claude

    chunks = []
    async for chunk in runner(
        prompt, cwd=SQUID_HOME, model=model,
        topic="_journal", agent=gen_agent, adhoc=True,
    ):
        if isinstance(chunk, str):
            chunks.append(chunk)
    return "".join(chunks).strip()


async def _generate_journal(
    topic: str, agent: Optional[str],
    week_key: str, week_start: str, week_end: str,
) -> Optional[Path]:
    scope = f"{topic}@{agent}" if agent else topic
    messages = get_topic_messages_for_period(topic, week_start, week_end, agent=agent)
    if len(messages) == 0:
        log.debug("journal skip %s — 0 turns", scope)
        return None

    prompt = _build_prompt(topic, agent, week_key, week_start, week_end, messages)
    try:
        text = await _run_generation(prompt)
    except Exception:
        log.exception("journal generation failed for %s", scope)
        return None

    if not text:
        log.warning("journal generation produced empty output for %s", scope)
        return None

    out_dir = _JOURNAL_DIR / scope
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{week_key}.md"
    out_path.write_text(text + "\n")
    log.info("journal written: %s (%d turns)", out_path, len(messages) // 2)

    state = _load_state()
    state[scope] = {
        "week": week_key,
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_state(state)
    return out_path


async def _generate_all_journal(week_key: str) -> Optional[Path]:
    existing = []
    if _JOURNAL_DIR.exists():
        for entry in sorted(_JOURNAL_DIR.iterdir()):
            if entry.name.startswith("_") or "@" in entry.name or not entry.is_dir():
                continue
            week_file = entry / f"{week_key}.md"
            if week_file.exists():
                existing.append((entry.name, week_file.read_text()))

    if not existing:
        return None

    year, w = week_key.split("-W")
    week_num = int(w)
    sections = "\n\n---\n\n".join(
        f"### Topic: #{name}\n{content}" for name, content in existing
    )

    prompt = f"""You are generating a cross-topic weekly summary for an AI coding assistant.

Below are individual topic journals for Week {week_num}, {year}. Synthesize them into a single high-level summary.
The output will be passed directly into a future agent's input as context.
Make it useful for handoff: preserve concrete project state, distinguish completed work from open threads, and explain how the next agent should use the summary.

<topic_journals>
{sections}
</topic_journals>

Produce ONLY the following Markdown, with no preamble:

# All Topics — Week {week_num}, {year}

## Overview
2–4 sentences summarizing the week across all topics.

## How to use this
Brief instructions for the next agent: which topic journals to consult first, what assumptions to verify, and which open threads are most likely to need follow-up.

## By topic
One bullet per topic with its key focus.

## Cross-cutting themes
Patterns, constraints, or lessons that appeared in multiple topics. If none, write "None noted."

## Open threads (all topics)
All deferred items across topics.

---
*Generated {datetime.date.today().isoformat()} by squid*"""

    try:
        text = await _run_generation(prompt)
    except Exception:
        log.exception("_all journal generation failed")
        return None

    if not text:
        return None

    out_dir = _JOURNAL_DIR / "_all"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{week_key}.md"
    out_path.write_text(text + "\n")
    log.info("_all journal written: %s", out_path)

    state = _load_state()
    state["_all"] = {
        "week": week_key,
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_state(state)
    return out_path


async def maybe_trigger_journals(topic: str, agent: Optional[str]) -> None:
    try:
        week_key, week_start, week_end = _current_week()
        state = _load_state()

        if _needs_generation(topic, week_key, state):
            await _generate_journal(topic, None, week_key, week_start, week_end)

        if agent:
            agent_scope = f"{topic}@{agent}"
            if _needs_generation(agent_scope, week_key, _load_state()):
                await _generate_journal(topic, agent, week_key, week_start, week_end)

        if _needs_generation("_all", week_key, _load_state()):
            await _generate_all_journal(week_key)

    except Exception:
        log.exception("maybe_trigger_journals failed for topic=%s agent=%s", topic, agent)


def list_topic_journals(topic: str) -> list[dict]:
    week_key, _, _ = _current_week()
    state = _load_state()
    result = []

    def _entry(scope: str, scope_agent: Optional[str], week_file: Path) -> dict:
        entry = state.get(scope, {})
        return {
            "scope": scope,
            "topic": topic,
            "agent": scope_agent,
            "week": week_file.stem,
            "path": str(week_file),
            "generated_at": entry.get("generated_at"),
            "current": week_file.stem == week_key,
        }

    agg_dir = _JOURNAL_DIR / topic
    if agg_dir.exists():
        for f in sorted(agg_dir.glob("*.md"), reverse=True):
            result.append(_entry(topic, None, f))

    if _JOURNAL_DIR.exists():
        for entry in sorted(_JOURNAL_DIR.iterdir()):
            if entry.name.startswith(f"{topic}@") and entry.is_dir():
                scope_agent = entry.name[len(topic) + 1:]
                for f in sorted(entry.glob("*.md"), reverse=True):
                    result.append(_entry(entry.name, scope_agent, f))

    return result


def read_journal(topic: str, week: str, agent: Optional[str] = None) -> Optional[str]:
    scope = f"{topic}@{agent}" if agent else topic
    path = _JOURNAL_DIR / scope / f"{week}.md"
    return path.read_text() if path.exists() else None
