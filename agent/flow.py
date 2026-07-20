"""
flow.py — Server-side Squid Flow (route chain) continuation.

Route chains (ADR-0032) execute one explicit turn per step, but the step
sequence itself must not depend on a browser tab staying open: TopicWorker
already runs every turn to completion independent of any connected client
(agent/topic_queue.py), persisting through to `chat_messages`/`run_events`.
This module hooks into that same completion point so the *next* chain step
is dispatched the same way, entirely server-side.

Only the currently-implemented subset of ADR-0032 is handled here: a linear
`#topic@origin[!]>@target[!]` one-way handoff (optionally crossing into a
different topic, `#topic@origin[!]>#other@target[!]`), or a single-round
`#topic@origin[!]<>@target[!]` request/response chain. Anything else (joins,
`<N>` for N != 1, scheduled edges, broadcasts) is not a chain this module
recognizes, and is left alone.
"""
import logging
import re
from typing import Optional

from .stats_db import (
    get_flow_run_messages,
    get_flow_run_id_for_message,
    get_flow_run_ids_with_row_counts,
)

log = logging.getLogger(__name__)

_ROUTE_CHAIN_RE = re.compile(r"^#(\w+)@(\w+)(!?)((?:<>)|>)(?:#(\w+))?@(\w+)(!?)$")


def chain_route_text(
    topic: str, origin_agent: str, target_agent: str,
    target_fresh: bool = False, origin_fresh: bool = False, operator: str = ">",
    target_topic: Optional[str] = None,
) -> str:
    target_prefix = f"#{target_topic}" if target_topic and target_topic != topic else ""
    return (
        f"#{topic}@{origin_agent}{'!' if origin_fresh else ''}{operator}"
        f"{target_prefix}@{target_agent}{'!' if target_fresh else ''}"
    )


def parse_route_chain(route: Optional[str]) -> Optional[dict]:
    """Port of the UI's parseRouteChain (ui/app.js) — must stay in sync with it."""
    match = _ROUTE_CHAIN_RE.match(str(route or ""))
    if not match:
        return None
    topic, origin, origin_fresh, operator_raw, target_topic, target, target_fresh = match.groups()
    operator = ">" if operator_raw == ">" else "<>"
    topic = topic.lower()
    target_topic = target_topic.lower() if target_topic else topic
    return {
        "topic": topic,
        "origin": origin,
        "origin_fresh": bool(origin_fresh),
        "operator": operator,
        "rounds": 0 if operator == ">" else 1,
        "target_topic": target_topic,
        "target": target,
        "target_fresh": bool(target_fresh),
        "route": chain_route_text(
            topic, origin, target, bool(target_fresh), bool(origin_fresh), operator, target_topic,
        ),
    }


def chain_handoff_prompt(route: str, previous_agent: str, next_agent: str, next_fresh: bool, original_prompt: str) -> str:
    """Port of the UI's chainHandoffPrompt (ui/app.js) — must stay in sync with it."""
    target_suffix = "!" if next_fresh else ""
    return "\n".join([
        "Squid route chain handoff.",
        f"Route: {route}",
        f"Previous step: @{previous_agent}",
        f"Current step: @{next_agent}{target_suffix}",
        f"Original prompt: {original_prompt}",
        "Previous output: injected context <previous_step_output>. Use it to continue.",
    ])


def next_chain_step(flow_run_id: str) -> Optional[dict]:
    """What to send next for this flow_run_id, or None if the chain is
    complete, stalled (last step not done/empty — fail-stop), or not a route
    chain this module understands.
    """
    rows = get_flow_run_messages(flow_run_id)
    if not rows:
        return None

    chain = parse_route_chain(rows[0].get("flow_route"))
    if not chain:
        return None

    last = rows[-1]
    if last["role"] != "assistant" or last["status"] != "done" or not (last["content"] or "").strip():
        return None

    original_prompt = rows[0]["content"]
    n = len(rows)

    if n == 2:
        return {
            "topic": chain["target_topic"],
            "agent": chain["target"],
            "fresh": chain["target_fresh"],
            "previous_agent": chain["origin"],
            "previous_msg_id": last["id"],
            "original_prompt": original_prompt,
            "route": chain["route"],
        }

    if n == 4 and chain["operator"] == "<>":
        return {
            "topic": chain["topic"],
            "agent": chain["origin"],
            "fresh": chain["origin_fresh"],
            "previous_agent": chain["target"],
            "previous_msg_id": last["id"],
            "original_prompt": original_prompt,
            "route": chain["route"],
        }

    return None  # chain complete: n==4 for '>', n==6 for '<>'


async def _dispatch_next_step(flow_run_id: str, step: dict) -> None:
    # Lazy import: server.py imports topic_queue.py at module scope, and
    # topic_queue.py calls into this module — importing server.py here (not
    # at module scope) avoids the import cycle.
    from .server import _prepare_chat_turn, stream_response
    from .context_sync import maybe_sync
    from fastapi.responses import JSONResponse

    prompt = chain_handoff_prompt(
        step["route"], step["previous_agent"], step["agent"], step["fresh"], step["original_prompt"],
    )
    prepared = await _prepare_chat_turn(
        message=prompt,
        topic=step["topic"],
        agent=step["agent"],
        adhoc=step["fresh"],
        lookback=0,
        pinned_ids=[step["previous_msg_id"]],
        source="system",
        flow_run_id=flow_run_id,
        flow_route=step["route"],
    )
    if isinstance(prepared, JSONResponse):
        log.warning("flow continuation failed to prepare turn: route=%s flow_run_id=%s", step["route"], flow_run_id)
        return

    log.info(
        "flow continuation dispatch route=%s flow_run_id=%s agent=%s msg_id=%s",
        step["route"], flow_run_id, step["agent"], prepared["asst_msg_id"],
    )
    await maybe_sync()
    async for _event in stream_response(
        prepared["effective_message"], prepared["topic"], prepared["agent"], prepared["backend"], prepared["model"], prepared["cwd"],
        prepared["context_history"], prepared["asst_msg_id"], prepared["response_timeout"],
        resume_session_id=prepared["resume_session_id"],
        adhoc=prepared["adhoc"],
        lookback=prepared["lookback"],
        code_roots=prepared["code_roots"],
        display_prompt=prepared["display_prompt"],
        source_cwd=prepared["source_cwd"],
        harness=prepared["harness"],
        provider=prepared["provider"],
    ):
        pass  # no live client — TopicWorker + this generator already persisted everything


async def continue_chain(msg_id: int) -> None:
    """Called after a message finishes successfully. If it's a step in a
    Squid Flow route chain with more steps to run, dispatch the next one —
    entirely server-side, independent of any connected browser tab."""
    try:
        flow_run_id = get_flow_run_id_for_message(msg_id)
        if not flow_run_id:
            return
        step = next_chain_step(flow_run_id)
        if not step:
            return
        # Re-check right before dispatch: guards against a boot-time sweep and
        # this same live hook racing each other for the same flow_run_id.
        rows_now = get_flow_run_messages(flow_run_id)
        if rows_now and rows_now[-1]["id"] != msg_id:
            return
        await _dispatch_next_step(flow_run_id, step)
    except Exception:
        log.exception("flow continuation failed for msg_id=%s", msg_id)


async def sweep_incomplete_flows() -> int:
    """Boot-time recovery: resume any Squid Flow chain whose last known step
    finished but whose next step was never dispatched — e.g. the server
    restarted mid-chain, or (pre-existing bug) the step used to depend on a
    client JS closure that never fired. Safe to call repeatedly."""
    resumed = 0
    try:
        for flow_run_id in get_flow_run_ids_with_row_counts((2, 4)):
            try:
                step = next_chain_step(flow_run_id)
            except Exception:
                log.exception("flow sweep failed to evaluate flow_run_id=%s", flow_run_id)
                continue
            if not step:
                continue
            log.warning("flow sweep resuming stalled chain flow_run_id=%s route=%s", flow_run_id, step["route"])
            try:
                await _dispatch_next_step(flow_run_id, step)
                resumed += 1
            except Exception:
                log.exception("flow sweep failed to dispatch flow_run_id=%s", flow_run_id)
    except Exception:
        log.exception("flow sweep failed")
    return resumed
