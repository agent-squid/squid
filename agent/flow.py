"""
flow.py — Server-side Squid Flow (route chain) continuation.

Route chains (ADR-0032) execute one explicit turn per step, but the step
sequence itself must not depend on a browser tab staying open: TopicWorker
already runs every turn to completion independent of any connected client
(agent/topic_queue.py), persisting through to `chat_messages`/`run_events`.
This module hooks into that same completion point so the *next* chain step
is dispatched the same way, entirely server-side.

Only the v0.1 live subset of ADR-0032 is handled here: zero or one immediate
handoff operator, with origin broadcasts, target fan-out, bare-topic targets,
origin-side joins, and single-round `<>`. Scheduled/repeated edges remain
playground/spec-only until a delayed/repeat dispatcher exists.
"""
import json
import logging
import re
import asyncio
from typing import Optional

from .stats_db import (
    get_flow_run_messages,
    get_flow_run_id_for_message,
    get_flow_run_ids_with_row_counts,
)

log = logging.getLogger(__name__)

_NAME = r"[A-Za-z0-9_.-]+"
_ATOM_RE = re.compile(rf"^(?:#({_NAME}))?(?:@({_NAME}))?(!?)$")
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_SCHEDULED_DISPATCHES: set[tuple] = set()


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


def _parse_atom(text: str) -> Optional[dict]:
    match = _ATOM_RE.match(text)
    if not match or (not match.group(1) and not match.group(2)):
        return None
    if match.group(3) and not match.group(2):
        return None
    return {
        "topic": match.group(1).lower() if match.group(1) else None,
        "agent": match.group(2) or None,
        "fresh": bool(match.group(3)),
    }


def _parse_group(text: str, allow_plus: bool) -> Optional[dict]:
    sep = "+" if "+" in text else "," if "," in text else None
    if "+" in text and "," in text:
        return None
    if sep == "+" and not allow_plus:
        return None
    raw_atoms = text.split(sep) if sep else [text]
    atoms = [_parse_atom(raw) for raw in raw_atoms]
    if any(atom is None for atom in atoms):
        return None
    return {"kind": "plus" if sep == "+" else "comma" if sep == "," else "single", "atoms": atoms}


def _resolve_origin_group(group: dict) -> Optional[list[dict]]:
    first = next((a for a in group["atoms"] if a["topic"] and a["agent"]), None)
    if not first:
        return None
    root = {"topic": first["topic"], "agent": first["agent"]}
    resolved = []
    for atom in group["atoms"]:
        if atom["topic"] and atom["agent"]:
            root = {"topic": atom["topic"], "agent": atom["agent"]}
        resolved.append({
            "topic": atom["topic"] or root["topic"],
            "agent": atom["agent"] or root["agent"],
            "fresh": atom["fresh"],
            "written": atom,
        })
    return resolved


def _join_forward_state(origins: list[dict]) -> dict:
    topics = {a["topic"] for a in origins}
    agents = {a["agent"] for a in origins}
    return {
        "topic": origins[0]["topic"] if len(topics) == 1 else None,
        "agent": origins[0]["agent"] if len(agents) == 1 else None,
    }


def _resolve_target_group(group: dict, state: dict) -> Optional[list[dict]]:
    resolved = []
    for atom in group["atoms"]:
        topic = atom["topic"] or state.get("topic")
        agent = atom["agent"] or state.get("agent")
        if not topic or not agent:
            return None
        resolved.append({"topic": topic, "agent": agent, "fresh": atom["fresh"], "written": atom})
    return resolved


def _minimal_grouped_text(atoms: list[dict], sep: str) -> str:
    remaining = atoms[:]
    runs = []
    while remaining:
        anchor = None
        anchor_cover = None
        for cand in remaining:
            cover = [a for a in remaining if a["topic"] == cand["topic"] or a["agent"] == cand["agent"]]
            better = (
                anchor is None
                or len(cover) > len(anchor_cover)
                or (
                    len(cover) == len(anchor_cover)
                    and (cand["topic"], cand["agent"]) < (anchor["topic"], anchor["agent"])
                )
            )
            if better:
                anchor = cand
                anchor_cover = cover
        runs.append({"anchor": anchor, "members": anchor_cover})
        covered = {id(a) for a in anchor_cover}
        remaining = [a for a in remaining if id(a) not in covered]
    runs.sort(key=lambda r: (r["anchor"]["topic"], r["anchor"]["agent"]))
    parts = []
    for run in runs:
        anchor = run["anchor"]
        parts.append(f"#{anchor['topic']}@{anchor['agent']}{'!' if anchor.get('fresh') else ''}")
        for atom in run["members"]:
            if atom is anchor:
                continue
            suffix = "!" if atom.get("fresh") else ""
            parts.append(f"@{atom['agent']}{suffix}" if atom["topic"] == anchor["topic"] else f"#{atom['topic']}{suffix}")
    return sep.join(parts)


def _drop_against_parent_text(atoms: list[dict], state: Optional[dict], sep: str) -> str:
    parts = []
    for atom in atoms:
        drop_topic = bool(state and state.get("topic") and state["topic"] == atom["topic"])
        drop_agent = bool(state and state.get("agent") and state["agent"] == atom["agent"])
        text = ""
        if not drop_topic:
            text += f"#{atom['topic']}"
        if not drop_agent:
            text += f"@{atom['agent']}"
        if not text:
            text = f"@{atom['agent']}"
        if atom.get("fresh"):
            text += "!"
        parts.append(text)
    return sep.join(parts)


def _parse_operator_token(op: str) -> Optional[dict]:
    # '>' and '=>' are both just the count=1/wait=None case of the scheduled
    # production below, spelled with the '=' omitted or the count+wait
    # omitted respectively — one type, not three (see ADR-0032, "Edge types").
    if op == ">":
        return {"type": "scheduled", "raw": op, "count": 1, "wait": None}
    match = re.fullmatch(r"<(\d+)?(?::(\d+[smhd]))?>", op)
    if match:
        return {
            "type": "roundtrip",
            "raw": op,
            "rounds": int(match.group(1) or "1"),
            "wait": match.group(2),
        }
    match = re.fullmatch(r"=(\d+)?(?::(\d+[smhd]))?>", op)
    if match:
        return {
            "type": "scheduled",
            "raw": op,
            "count": int(match.group(1) or "1"),
            "wait": match.group(2),
        }
    return None


def _op_to_text(op: dict) -> str:
    """Canonical (shortest) spelling for a parsed operator — port of the UI's
    opToText/canonicalKeyForClause roundtrip branch (ui/flow-lang.js); must
    stay in sync with it. Never echoes `op["raw"]` (whatever the route was
    literally typed as) — two spellings of the same edge (e.g. '=1>' and
    '=>') must canonicalize identically so they dedup to the same key."""
    if op["type"] == "roundtrip":
        rounds = op.get("rounds", 1)
        wait = op.get("wait")
        if rounds == 1 and not wait:
            return "<>"
        return f"<{rounds}{':' + wait if wait else ''}>"
    count = op.get("count", 1)
    wait = op.get("wait")
    if count == 1 and not wait:
        return ">"
    n = "" if count == 1 else str(count)
    t = f":{wait}" if wait else ""
    return f"={n}{t}>"


def _split_operator(route: str) -> Optional[tuple[str, dict, str]]:
    match = re.search(r"(?:<\d*(?::\d+[smhd])?>|=\d*(?::\d+[smhd])?>|>)", route)
    if not match:
        return None
    op = _parse_operator_token(match.group(0))
    if not op:
        return None
    lhs = route[:match.start()]
    rhs = route[match.end():]
    if not lhs or not rhs:
        return None
    if re.search(r"(?:<\d*(?::\d+[smhd])?>|=\d*(?::\d+[smhd])?>|>)", rhs):
        return None
    return lhs, op, rhs


def parse_flow_route(route: Optional[str]) -> Optional[dict]:
    """Parse the live v0.1 Squid Flow subset. Stored route text is reduced."""
    text = re.sub(r"\s+", "", str(route or ""))
    split = _split_operator(text)
    if not split:
        return None
    origin_text, operator, target_text = split
    origin_group = _parse_group(origin_text, allow_plus=True)
    # A round-trip's return leg is a real consumer of a joined target (see
    # ADR-0032, "Principle of join") — every other operator is terminal, so
    # '+' stays illegal on their target side.
    target_group = _parse_group(target_text, allow_plus=(operator["type"] == "roundtrip"))
    if not origin_group or not target_group:
        return None
    origins = _resolve_origin_group(origin_group)
    if not origins:
        return None
    if operator["type"] == "roundtrip" and origin_group["kind"] == "plus":
        return None
    target_join = operator["type"] == "roundtrip" and target_group["kind"] == "plus"

    branches = []
    if origin_group["kind"] == "plus":
        targets = _resolve_target_group(target_group, _join_forward_state(origins))
        if not targets:
            return None
        if target_join:
            branches.append({"origins": origins, "targets": targets, "target_join": True, "operator": operator["type"], "op": operator})
        else:
            for target in targets:
                branches.append({"origins": origins, "targets": [target], "target_join": False, "operator": operator["type"], "op": operator})
    else:
        for origin_atom in origins:
            targets = _resolve_target_group(target_group, origin_atom)
            if not targets:
                return None
            if target_join:
                branches.append({"origins": [origin_atom], "targets": targets, "target_join": True, "operator": operator["type"], "op": operator})
            else:
                for target in targets:
                    branches.append({
                        "origins": [origin_atom],
                        "targets": [target],
                        "target_join": False,
                        "operator": operator["type"],
                        "op": operator,
                    })

    origin_sep = "+" if origin_group["kind"] == "plus" else ","
    key = _minimal_grouped_text(origins, origin_sep)
    if origin_group["kind"] == "plus":
        uniform_state = _join_forward_state(origins)
    else:
        topics = {a["topic"] for a in origins}
        agents = {a["agent"] for a in origins}
        uniform_state = {
            "topic": origins[0]["topic"] if len(topics) == 1 else None,
            "agent": origins[0]["agent"] if len(agents) == 1 else None,
        }
    key_targets = _resolve_target_group(target_group, uniform_state)
    target_sep = "+" if target_group["kind"] == "plus" else ","
    op_text = _op_to_text(operator)
    if key_targets:
        key += op_text + _drop_against_parent_text(key_targets, uniform_state, target_sep)
    else:
        key += op_text + target_text

    first_origin = origins[0]
    first_target = branches[0]["targets"][0]
    return {
        "topic": first_origin["topic"],
        "origin": first_origin["agent"],
        "origin_fresh": bool(first_origin["fresh"]),
        "operator": "<>" if operator["type"] == "roundtrip" else ">",
        "operator_raw": operator["raw"],
        "rounds": operator.get("rounds", 0) if operator["type"] == "roundtrip" else 0,
        "target_topic": first_target["topic"],
        "target": first_target["agent"],
        "target_fresh": bool(first_target["fresh"]),
        "origins": origins,
        "branches": branches,
        "join": origin_group["kind"] == "plus",
        "target_join": target_join,
        "route": key,
    }


def chain_handoff_prompt(route: str, previous_agent: str, next_agent: str, next_fresh: bool, original_prompt: str) -> str:
    """Port of the UI's chainHandoffPrompt (ui/app.js) — must stay in sync with it."""
    target_suffix = "!" if next_fresh else ""
    previous_label = previous_agent if previous_agent.startswith("@") else f"@{previous_agent}"
    return "\n".join([
        "Squid route chain handoff.",
        f"Route: {route}",
        f"Previous step: {previous_label}",
        f"Current step: @{next_agent}{target_suffix}",
        f"Original prompt: {original_prompt}",
        "Previous output: injected context <previous_step_output>. Use it to continue.",
    ])


def _done_assistant_for_user(rows: list[dict], user_id: int) -> Optional[dict]:
    for row in rows:
        if row["role"] == "assistant" and row.get("reply_to") == user_id:
            if row["status"] == "done" and (row.get("content") or "").strip():
                return row
            return None
    return None


def _origin_assistant(rows: list[dict], origin: dict, claimed: set[int], resolved: dict[int, Optional[int]]) -> Optional[dict]:
    # Two *different* origin atoms (distinct dict objects — one per comma/plus
    # entry in the origin group) can resolve to the identical (topic, agent),
    # e.g. `#t@a!,@a!` under the rolling anchor (ADR-0032). Matching by value
    # alone would point both at the same (first) row. But the same single
    # origin atom is also legitimately *shared* across multiple branches for
    # target fan-out (`#a>@b,@c` — one origin atom, two branches) and must
    # keep resolving to that one row every time it's looked up, not grab a
    # second distinct row.
    #
    # `resolved` memoizes by origin-atom identity (id(origin)) so a shared
    # atom always maps to the same row; `claimed` tracks which rows are
    # already spoken for by some *other* atom, so distinct-but-equal atoms
    # each claim their own row in turn. (Can't also match on origin["fresh"]
    # here — insert_user_message never writes the `adhoc` column, so every
    # user row reads back adhoc=0 regardless of whether it was a fresh send.)
    key = id(origin)
    if key in resolved:
        row_id = resolved[key]
        return _done_assistant_for_user(rows, row_id) if row_id is not None else None
    for row in rows:
        if row["role"] != "user" or row.get("source") != "human":
            continue
        if row["id"] in claimed:
            continue
        if row["topic"] == origin["topic"] and row["agent"] == origin["agent"]:
            claimed.add(row["id"])
            resolved[key] = row["id"]
            return _done_assistant_for_user(rows, row["id"])
    resolved[key] = None
    return None


def _row_pins(row: dict) -> Optional[frozenset[int]]:
    """The message ids this row was dispatched with as pinned context
    (server.py stores step["previous_msg_ids"] into insert_user_message's
    context_ids, so every flow-dispatched target/return row carries exactly
    its own branch's previous_ids here). None means "no pin info" — either a
    human-authored row, or one from before this field existed — which callers
    should treat as inconclusive rather than a non-match."""
    raw = row.get("context")
    if not raw:
        return None
    try:
        pins = json.loads(raw).get("pins")
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(pins, list) or not pins:
        return None
    return frozenset(pins)


def _row_belongs_to_branch(row: dict, previous_ids: Optional[list[int]]) -> bool:
    """Whether `row` is plausibly *this* branch's own dispatched turn, not a
    sibling branch's — needed because two branches can share the identical
    (topic, agent) target (rolling-anchor duplicate origins resolve their
    targets identically too), which makes the topic/agent/after_id filters in
    _system_users/_system_assistant/_system_user_exists alone ambiguous.
    Falls back to "yes" when either side has no pin info to compare, so the
    ordinary single-branch case (or a manually-typed human message) is
    unaffected."""
    if not previous_ids:
        return True
    pins = _row_pins(row)
    if pins is None:
        return True
    return pins == frozenset(previous_ids)


def _system_user_exists(rows: list[dict], *, after_id: int, topic: str, agent: str, route: str, exclude_ids: frozenset[int] = frozenset(), previous_ids: Optional[list[int]] = None) -> bool:
    # exclude_ids: this chain's own origin rows (see next_chain_steps) — a
    # degenerate route can give a target the identical (topic, agent) as
    # another branch's origin (e.g. rolling-anchor duplicates), and that
    # origin's human-authored user row must never be mistaken for a
    # already-dispatched target leg just because it shares topic/agent.
    route_line = f"Route: {route}"
    for row in rows:
        if row["id"] <= after_id or row["role"] != "user" or row["id"] in exclude_ids:
            continue
        if not _row_belongs_to_branch(row, previous_ids):
            continue
        content = row.get("content") or ""
        if row["topic"] == topic and row["agent"] == agent and (route_line in content or row.get("source") != "system"):
            return True
    return False


def _system_assistant(rows: list[dict], *, after_id: int, topic: str, agent: str, route: str, exclude_ids: frozenset[int] = frozenset(), previous_ids: Optional[list[int]] = None) -> Optional[dict]:
    route_line = f"Route: {route}"
    for user in rows:
        if user["id"] <= after_id or user["role"] != "user" or user["id"] in exclude_ids:
            continue
        if not _row_belongs_to_branch(user, previous_ids):
            continue
        content = user.get("content") or ""
        if user["topic"] != topic or user["agent"] != agent:
            continue
        if user.get("source") == "system" and route_line not in content:
            continue
        return _done_assistant_for_user(rows, user["id"])
    return None


def _system_users(rows: list[dict], *, after_id: int, topic: str, agent: str, route: str, exclude_ids: frozenset[int] = frozenset(), previous_ids: Optional[list[int]] = None) -> list[dict]:
    route_line = f"Route: {route}"
    out = []
    for row in rows:
        if row["id"] <= after_id or row["role"] != "user" or row["id"] in exclude_ids:
            continue
        if not _row_belongs_to_branch(row, previous_ids):
            continue
        content = row.get("content") or ""
        if row["topic"] != topic or row["agent"] != agent:
            continue
        if row.get("source") == "system" and route_line not in content:
            continue
        out.append(row)
    return out


def _duration_seconds(wait: Optional[str]) -> int:
    if not wait:
        return 0
    match = _DURATION_RE.match(wait)
    if not match:
        return 0
    value = int(match.group(1))
    unit = match.group(2)
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _step(
    *,
    chain: dict,
    target: dict,
    previous_label: str,
    previous_ids: list[int],
    original_prompt: str,
    delay_seconds: int = 0,
    schedule_key: Optional[tuple] = None,
) -> dict:
    return {
        "topic": target["topic"],
        "agent": target["agent"],
        "fresh": target["fresh"],
        "previous_agent": previous_label,
        "previous_msg_ids": previous_ids,
        "original_prompt": original_prompt,
        "route": chain["route"],
        "delay_seconds": delay_seconds,
        "schedule_key": schedule_key,
    }


def next_chain_steps(flow_run_id: str) -> list[dict]:
    """What to send next for this flow_run_id, or None if the chain is
    complete, stalled (last step not done/empty — fail-stop), or not a route
    chain this module understands.
    """
    rows = get_flow_run_messages(flow_run_id)
    if not rows:
        return []

    chain = parse_flow_route(rows[0].get("flow_route"))
    if not chain:
        return []

    original_prompt = rows[0]["content"]
    ready = []
    # Both shared across every branch below (not reset per-branch) — see
    # _origin_assistant for why a memoized identity map and a claimed-rows
    # set are both needed.
    claimed_origins: set[int] = set()
    resolved_origins: dict[int, Optional[int]] = {}
    # Resolve every branch's origin(s) up front so claimed_origins is
    # complete (the full set of rows that are *this chain's own origins*)
    # before any branch below checks whether one of its target legs was
    # already dispatched — otherwise a branch processed early wouldn't yet
    # know about a later branch's origin row and could mistake it for a
    # dispatched target (see _system_users/_system_assistant exclude_ids).
    for branch in chain["branches"]:
        for origin in branch["origins"]:
            _origin_assistant(rows, origin, claimed_origins, resolved_origins)

    for branch in chain["branches"]:
        origin_assistants = [_origin_assistant(rows, origin, claimed_origins, resolved_origins) for origin in branch["origins"]]
        if any(row is None for row in origin_assistants):
            continue
        previous_ids = [row["id"] for row in origin_assistants]
        previous_label = branch["origins"][0]["agent"] if len(branch["origins"]) == 1 else "+".join(f"@{origin['agent']}" for origin in branch["origins"])
        # scheduled branches always carry exactly one target (target fan-out
        # decomposes into one branch per target at parse time); only a
        # round-trip branch can have more than one, via target_join.
        target = branch["targets"][0]
        op = branch["op"]
        after_origin = max(previous_ids)

        if op["type"] == "scheduled":
            sent = len(_system_users(rows, after_id=after_origin, topic=target["topic"], agent=target["agent"], route=chain["route"], exclude_ids=claimed_origins, previous_ids=previous_ids))
            delay_unit = _duration_seconds(op.get("wait"))
            for i in range(sent, op["count"]):
                schedule_key = (flow_run_id, chain["route"], "scheduled", target["topic"], target["agent"], tuple(previous_ids), i + 1)
                if schedule_key in _SCHEDULED_DISPATCHES:
                    continue
                ready.append(_step(
                    chain=chain,
                    target=target,
                    previous_label=previous_label,
                    previous_ids=previous_ids,
                    original_prompt=original_prompt,
                    delay_seconds=delay_unit * (i + 1) if delay_unit else 0,
                    # Always set — including for an immediate (zero-delay)
                    # dispatch. Several messages in a multi-origin chain can
                    # each complete and call continue_chain() within the same
                    # narrow window, and each independently calls
                    # next_chain_steps() and can conclude the same branch is
                    # still undispatched before any of them has committed a
                    # row proving otherwise. schedule_key is how
                    # _dispatch_or_schedule claims a step atomically (no
                    # `await` between the check and the claim) so only the
                    # first of those racing calls actually dispatches.
                    schedule_key=schedule_key,
                ))
            continue

        if op["type"] != "roundtrip":
            continue

        origin = branch["origins"][0]
        current_previous_ids = previous_ids
        current_after = after_origin
        current_previous_label = previous_label
        wait_seconds = _duration_seconds(op.get("wait"))
        for leg in range(op.get("rounds", 1) * 2):
            # The "out" leg is every joined target at once for a target_join
            # branch (dispatched in parallel), or the single target
            # otherwise; the "return" leg is always the single origin.
            leg_targets = branch["targets"] if leg % 2 == 0 else [origin]
            done = []
            pending = []
            for leg_target in leg_targets:
                assistant = _system_assistant(rows, after_id=current_after, topic=leg_target["topic"], agent=leg_target["agent"], route=chain["route"], exclude_ids=claimed_origins, previous_ids=current_previous_ids)
                (done if assistant else pending).append((leg_target, assistant))
            if not pending:
                # Every member of this leg completed — advance past it,
                # pinning all of them (mirrors the origin-side join gate above).
                current_previous_ids = [assistant["id"] for _, assistant in done]
                current_after = max(current_previous_ids)
                current_previous_label = (
                    leg_targets[0]["agent"] if len(leg_targets) == 1
                    else "+".join(f"@{t['agent']}" for t in leg_targets)
                )
                continue
            # Not every member is done yet — dispatch whichever haven't been
            # sent, in parallel, and leave the rest for a later call (each
            # completion re-triggers next_chain_steps via the completion hook).
            for leg_target, _ in pending:
                if _system_user_exists(rows, after_id=current_after, topic=leg_target["topic"], agent=leg_target["agent"], route=chain["route"], exclude_ids=claimed_origins, previous_ids=current_previous_ids):
                    continue
                schedule_key = (flow_run_id, chain["route"], "roundtrip", leg_target["topic"], leg_target["agent"], tuple(current_previous_ids), leg + 1)
                if schedule_key in _SCHEDULED_DISPATCHES:
                    continue
                ready.append(_step(
                    chain=chain,
                    target=leg_target,
                    previous_label=current_previous_label,
                    previous_ids=current_previous_ids,
                    original_prompt=original_prompt,
                    delay_seconds=wait_seconds,
                    # Always set — see the scheduled branch above for why.
                    schedule_key=schedule_key,
                ))
            break
    return ready


def next_chain_step(flow_run_id: str) -> Optional[dict]:
    steps = next_chain_steps(flow_run_id)
    if not steps:
        return None
    step = dict(steps[0])
    previous_ids = step.pop("previous_msg_ids", [])
    if len(previous_ids) == 1:
        step["previous_msg_id"] = previous_ids[0]
    else:
        step["previous_msg_ids"] = previous_ids
    if not step.get("delay_seconds"):
        step.pop("delay_seconds", None)
    # schedule_key is next_chain_steps/_dispatch_or_schedule's internal
    # dedup claim (always set now, even for an immediate dispatch — see
    # next_chain_steps) — not part of this legacy-compatible single-step
    # summary shape.
    step.pop("schedule_key", None)
    return step


def expected_row_count(route: Optional[str]) -> int:
    chain = parse_flow_route(route)
    if not chain:
        return 0
    origin_ids = {
        id(origin)
        for branch in chain["branches"]
        for origin in branch["origins"]
    }
    count = len(origin_ids) * 2
    for branch in chain["branches"]:
        op = branch["op"]
        if op["type"] == "scheduled":
            # count=1/wait=None ('>'/'=>') falls out of this the same as any
            # other repeat count — no separate case needed.
            count += 2 * op["count"]
        else:  # roundtrip
            # Each round is every target (2 rows apiece — usually one, but a
            # target_join branch dispatches all of them in parallel) plus one
            # return leg (2 rows); reduces to the old `4 * rounds` when there's
            # only one target.
            count += op.get("rounds", 1) * (len(branch["targets"]) * 2 + 2)
    return count


def parse_route_chain(route: Optional[str]) -> Optional[dict]:
    """Legacy-compatible summary of a live Squid Flow route."""
    chain = parse_flow_route(route)
    if not chain:
        return None
    return {
        "topic": chain["topic"],
        "origin": chain["origin"],
        "origin_fresh": chain["origin_fresh"],
        "operator": chain["operator"],
        "rounds": chain["rounds"],
        "target_topic": chain["target_topic"],
        "target": chain["target"],
        "target_fresh": chain["target_fresh"],
        "route": chain["route"],
    }


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
        pinned_ids=step["previous_msg_ids"],
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


async def _dispatch_or_schedule(flow_run_id: str, step: dict) -> bool:
    delay = int(step.get("delay_seconds") or 0)
    schedule_key = step.get("schedule_key")
    # Claim synchronously — check-then-add with no `await` between them, so
    # this can't interleave with another coroutine doing the same check for
    # the identical step (see next_chain_steps' scheduled branch for why
    # that race is real: several messages in one multi-origin chain can each
    # complete and independently decide the same branch still needs
    # dispatching before any of them has committed proof otherwise). Only
    # the first caller to reach this line for a given schedule_key wins.
    if schedule_key in _SCHEDULED_DISPATCHES:
        return False
    _SCHEDULED_DISPATCHES.add(schedule_key)
    if delay <= 0:
        try:
            await _dispatch_next_step(flow_run_id, step)
        finally:
            _SCHEDULED_DISPATCHES.discard(schedule_key)
        return True

    async def later() -> None:
        try:
            await asyncio.sleep(delay)
            await _dispatch_next_step(flow_run_id, step)
        except Exception:
            log.exception("delayed flow dispatch failed flow_run_id=%s route=%s", flow_run_id, step.get("route"))
        finally:
            _SCHEDULED_DISPATCHES.discard(schedule_key)

    asyncio.create_task(later(), name=f"squid-flow-delay-{flow_run_id}")
    return True


async def continue_chain(msg_id: int) -> None:
    """Called after a message finishes successfully. If it's a step in a
    Squid Flow route chain with more steps to run, dispatch the next one —
    entirely server-side, independent of any connected browser tab."""
    try:
        flow_run_id = get_flow_run_id_for_message(msg_id)
        if not flow_run_id:
            return
        steps = next_chain_steps(flow_run_id)
        if not steps:
            return
        # No "is msg_id still the last row" re-check here (there used to be
        # one) — that was only ever valid for a single linear branch, where
        # nothing else could complete concurrently. A join's origins run in
        # parallel and are dispatched (and therefore id-ordered) up front,
        # so they can *finish* in any order: whichever origin finishes last
        # chronologically is not guaranteed to hold the highest id, and that
        # stale check silently dropped the dispatch whenever it didn't (the
        # join would then never fire its target). The real
        # boot-sweep-vs-live-hook race is already handled per-step by
        # _dispatch_or_schedule's atomic schedule_key claim below.
        for step in steps:
            await _dispatch_or_schedule(flow_run_id, step)
    except Exception:
        log.exception("flow continuation failed for msg_id=%s", msg_id)


async def sweep_incomplete_flows() -> int:
    """Boot-time recovery: resume any Squid Flow chain whose last known step
    finished but whose next step was never dispatched — e.g. the server
    restarted mid-chain, or (pre-existing bug) the step used to depend on a
    client JS closure that never fired. Safe to call repeatedly."""
    resumed = 0
    try:
        for flow_run_id in get_flow_run_ids_with_row_counts(tuple(range(2, 41))):
            try:
                steps = next_chain_steps(flow_run_id)
            except Exception:
                log.exception("flow sweep failed to evaluate flow_run_id=%s", flow_run_id)
                continue
            if not steps:
                continue
            log.warning("flow sweep resuming stalled chain flow_run_id=%s route=%s", flow_run_id, steps[0]["route"])
            try:
                for step in steps:
                    if await _dispatch_or_schedule(flow_run_id, step):
                        resumed += 1
            except Exception:
                log.exception("flow sweep failed to dispatch flow_run_id=%s", flow_run_id)
    except Exception:
        log.exception("flow sweep failed")
    return resumed
