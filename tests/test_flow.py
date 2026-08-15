import asyncio
import json
from unittest.mock import patch, AsyncMock

import pytest

from agent import flow
from agent import server
from agent import stats_db
from agent.config import TEST_HARNESS_ENABLED


def _seed_agent(name):
    stats_db.upsert_agent(name, "codex", None, None, cwd="/tmp/project")


def test_durable_flow_plan_compiles_repeats_and_roundtrip_dependencies():
    scheduled = flow.durable_flow_plan("#squid@codex=2:5m>@review")

    assert [step["step_id"] for step in scheduled] == [
        "origin:0", "branch:0:target:0", "branch:0:target:1",
    ]
    assert scheduled[1]["dependencies"] == ["origin:0"]
    assert scheduled[1]["delay_seconds"] == 300
    assert scheduled[2]["delay_seconds"] == 600
    assert scheduled[0]["branch_index"] == -1

    roundtrip = flow.durable_flow_plan("#squid@codex<2>@review")
    assert roundtrip[1]["dependencies"] == ["origin:0"]
    assert roundtrip[2]["dependencies"] == ["branch:0:round:0:target:0"]
    assert roundtrip[3]["dependencies"] == ["branch:0:round:0:return"]
    assert roundtrip[4]["dependencies"] == ["branch:0:round:1:target:0"]

    fanout = flow.durable_flow_plan("#squid@codex>@review,@test")
    assert [step["branch_index"] for step in fanout if step["leg"] == "origin"] == [-1]


def test_durable_flow_cutover_links_origin_and_claims_next_step(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex>@review"
    user_id = stats_db.insert_user_message(
        "squid", "codex", "review this", flow_run_id="run-1", flow_route=route,
    )
    assistant_id = stats_db.insert_assistant_message(
        "squid", "codex", user_id, flow_run_id="run-1", flow_route=route,
    )
    prepared = {
        "topic": "squid", "agent": "codex",
        "user_msg_id": user_id, "asst_msg_id": assistant_id,
    }

    assert server._persist_flow_plan("run-1", route, prepared) is None
    run = stats_db.get_flow_run("run-1")
    steps = {step["step_id"]: step for step in stats_db.get_flow_steps("run-1")}
    assert run["execution_mode"] == "durable"
    assert steps["origin:0"]["status"] == "running"
    assert steps["origin:0"]["user_msg_id"] == user_id
    assert steps["origin:0"]["assistant_msg_id"] == assistant_id
    assert stats_db.get_message(assistant_id)["flow_step_id"] == "origin:0"

    async def run_completion():
        with patch.object(
            flow, "_dispatch_claimed_durable_step", new=AsyncMock(return_value=True),
        ) as dispatch:
            handled = await flow.complete_durable_step(assistant_id)
            return handled, dispatch

    handled, dispatch = asyncio.run(run_completion())
    assert handled
    steps = {step["step_id"]: step for step in stats_db.get_flow_steps("run-1")}
    assert steps["origin:0"]["status"] == "done"
    assert steps["branch:0:target:0"]["status"] == "claimed"
    dispatch.assert_awaited_once()

    joined = flow.durable_flow_plan("#squid@codex+@review>@test")
    assert [step["branch_index"] for step in joined if step["leg"] == "origin"] == [-1, -1]


def _seed_chain(route, rows):
    """rows: list of (role, agent, content, status) tuples, built in order.
    role='user' inserts a user message; role='assistant' replies to the most
    recent user message. Returns the flow_run_id used and the id of the last
    inserted row."""
    flow_run_id = "f1"
    last_user_id = None
    last_id = None
    for role, agent, content, status in rows:
        if role == "user":
            last_user_id = stats_db.insert_user_message(
                "squid", agent, content, flow_run_id=flow_run_id, flow_route=route,
            )
            last_id = last_user_id
        else:
            asst_id = stats_db.insert_assistant_message(
                "squid", agent, last_user_id, flow_run_id=flow_run_id, flow_route=route,
            )
            stats_db.update_assistant_message(asst_id, content, None, status)
            last_id = asst_id
    return flow_run_id, last_id


# ---------------------------------------------------------------------------
# parse_route_chain / chain_route_text
# ---------------------------------------------------------------------------

def test_parse_route_chain_one_way():
    assert flow.parse_route_chain("#squid@codex>@revucla") == {
        "topic": "squid",
        "origin": "codex",
        "origin_fresh": False,
        "operator": ">",
        "rounds": 0,
        "target_topic": "squid",
        "target": "revucla",
        "target_fresh": False,
        "route": "#squid@codex>@revucla",
    }


def test_parse_route_chain_fresh_flags_and_round_trip():
    assert flow.parse_route_chain("#squid@codex!<>@revucla!") == {
        "topic": "squid",
        "origin": "codex",
        "origin_fresh": True,
        "operator": "<>",
        "rounds": 1,
        "target_topic": "squid",
        "target": "revucla",
        "target_fresh": True,
        "route": "#squid@codex!<>@revucla!",
    }


def test_parse_route_chain_cross_topic_target():
    assert flow.parse_route_chain("#squid@codex>#hive@revucla!") == {
        "topic": "squid",
        "origin": "codex",
        "origin_fresh": False,
        "operator": ">",
        "rounds": 0,
        "target_topic": "hive",
        "target": "revucla",
        "target_fresh": True,
        "route": "#squid@codex>#hive@revucla!",
    }


def test_parse_route_chain_bare_topic_target_inherits_origin_agent():
    assert flow.parse_route_chain("#squid@codex>#hive") == {
        "topic": "squid",
        "origin": "codex",
        "origin_fresh": False,
        "operator": ">",
        "rounds": 0,
        "target_topic": "hive",
        "target": "codex",
        "target_fresh": False,
        "route": "#squid@codex>#hive",
    }


def test_parse_route_chain_same_topic_target_prefix_is_dropped_on_render():
    # A redundant same-topic `#topic` prefix on the target still parses, but
    # the canonical route text collapses it back to the compact bare form.
    chain = flow.parse_route_chain("#squid@codex>#squid@revucla")
    assert chain["target_topic"] == "squid"
    assert chain["route"] == "#squid@codex>@revucla"


def test_parse_route_chain_rejects_unsupported_or_malformed():
    assert flow.parse_route_chain(None) is None
    assert flow.parse_route_chain("") is None
    assert flow.parse_route_chain("#squid@codex") is None
    assert flow.parse_route_chain("#squid@codex@bad>@revucla") is None


def test_parse_route_chain_repeated_rounds():
    chain = flow.parse_route_chain("#squid@codex<2>@revucla")
    assert chain["operator"] == "<>"
    assert chain["rounds"] == 2
    assert chain["route"] == "#squid@codex<2>@revucla"


def test_parse_flow_route_forward_is_one_type_not_two():
    # '>', '=>', and '=1>' are the same edge (count=1, wait=None) — not two
    # types (oneway vs scheduled) that happen to look similar. See ADR-0032,
    # "Edge types": the only real types are forward ('=N:T>', shorthand '>')
    # and roundtrip ('<N:T>').
    variants = ["#squid@codex>@revucla", "#squid@codex=>@revucla", "#squid@codex=1>@revucla"]
    parsed = [flow.parse_flow_route(v) for v in variants]
    for chain in parsed:
        assert chain["operator"] == ">"
        assert chain["rounds"] == 0
        assert chain["route"] == "#squid@codex>@revucla"
        op = chain["branches"][0]["op"]
        assert op == {"type": "scheduled", "raw": op["raw"], "count": 1, "wait": None}


def test_parse_flow_route_forward_count_omitted_with_wait():
    # New: count can be omitted even when a wait is present, same as
    # roundtrip's '<:T>' — '=:5m>' means count=1, wait='5m'.
    chain = flow.parse_flow_route("#squid@codex=:5m>@revucla")
    op = chain["branches"][0]["op"]
    assert op["count"] == 1
    assert op["wait"] == "5m"
    # Canonicalizes the same way regardless of whether count=1 was written
    # explicitly or omitted — shortest spelling wins.
    assert chain["route"] == "#squid@codex=:5m>@revucla"
    assert flow.parse_flow_route("#squid@codex=1:5m>@revucla")["route"] == "#squid@codex=:5m>@revucla"


def test_next_chain_steps_forward_variants_dispatch_identically(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr("agent.server.WORKTREE_ISOLATION_ENABLED", False)
    monkeypatch.setattr("agent.config.WORKTREE_ISOLATION_ENABLED", False)
    stats_db.init_db()
    # _seed_chain hardcodes flow_run_id="f1" — insert directly with a
    # distinct id per variant so each iteration starts from a clean chain.
    for i, route in enumerate(["#squid@codex>@revucla", "#squid@codex=>@revucla", "#squid@codex=1>@revucla"]):
        flow_run_id = f"f{i}"
        user_id = stats_db.insert_user_message("squid", "codex", "review this", flow_run_id=flow_run_id, flow_route=route)
        asst_id = stats_db.insert_assistant_message("squid", "codex", user_id, flow_run_id=flow_run_id, flow_route=route)
        stats_db.update_assistant_message(asst_id, "codex output", None, "done")
        steps = flow.next_chain_steps(flow_run_id)
        assert len(steps) == 1
        step = steps[0]
        assert step["topic"] == "squid"
        assert step["agent"] == "revucla"
        assert step["previous_msg_ids"] == [asst_id]
        # No delay for any of these, but schedule_key is still set even for
        # an immediate dispatch — it's how _dispatch_or_schedule claims a
        # step atomically against a sibling branch racing the same decision
        # (see next_chain_steps' scheduled branch).
        assert not step.get("delay_seconds")
        assert step.get("schedule_key") is not None


def test_parse_flow_route_rejects_target_join_without_roundtrip():
    # '+' on a hop's target is only a consumer when the round-trip return
    # leg is there to feed — plain '>' has no consumer for it (ADR-0032,
    # "Principle of join").
    assert flow.parse_flow_route("#t1@a1>#t2+#t3") is None
    assert flow.parse_flow_route("#t1@a1=>#t2+#t3") is None
    assert flow.parse_flow_route("#t1@a1=2>#t2+#t3") is None


def test_parse_flow_route_target_join_under_roundtrip():
    chain = flow.parse_flow_route("#t1@a1<>#t2+#t3")
    assert chain["target_join"] is True
    assert chain["join"] is False
    assert chain["route"] == "#t1@a1<>#t2+#t3"
    assert len(chain["branches"]) == 1
    branch = chain["branches"][0]
    assert branch["target_join"] is True
    assert [(t["topic"], t["agent"]) for t in branch["targets"]] == [("t2", "a1"), ("t3", "a1")]


def test_next_chain_steps_target_fanout_after_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex>@review,@test"
    flow_run_id, asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    steps = flow.next_chain_steps(flow_run_id)
    assert [
        (s["topic"], s["agent"], s["previous_msg_ids"], s["route"])
        for s in steps
    ] == [
        ("squid", "review", [asst_id], route),
        ("squid", "test", [asst_id], route),
    ]


def test_next_chain_steps_duplicate_origin_atoms_each_keep_their_own_assistant(tmp_path, monkeypatch):
    # #squid@qwen!,@qwen! — two distinct origin sends that both resolve
    # (rolling anchor, ADR-0032) to the identical (topic, agent). Each is its
    # own branch and must advance off its *own* origin assistant, not both
    # collapsing onto whichever origin row happens to be first.
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@qwen!,@qwen!>@qwen!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "qwen", "go", None),
        ("assistant", "qwen", "qwen output 1", "done"),
        ("user", "qwen", "go", None),
        ("assistant", "qwen", "qwen output 2", "done"),
    ])
    rows = stats_db.get_flow_run_messages(flow_run_id)
    asst_ids = sorted(r["id"] for r in rows if r["role"] == "assistant")
    assert len(asst_ids) == 2

    steps = flow.next_chain_steps(flow_run_id)
    assert len(steps) == 2
    assert sorted(s["previous_msg_ids"][0] for s in steps) == asst_ids
    # The two steps must not both key off the same origin assistant.
    assert steps[0]["previous_msg_ids"] != steps[1]["previous_msg_ids"]
    for s in steps:
        assert (s["topic"], s["agent"]) == ("squid", "qwen")


def test_expected_row_count_counts_duplicate_origin_atoms_but_not_fanout_origins():
    # Duplicate comma origins are distinct turns even when they resolve to the
    # same topic/agent/fresh tuple, so the flow watcher must not stop after
    # the first child leg.
    assert flow.expected_row_count("#squid@qwen!,@qwen!>@qwen!") == 8

    # Target fanout reuses one origin turn across two branches; that origin is
    # still only one user/assistant pair.
    assert flow.expected_row_count("#squid@qwen!>@a,@b") == 6


def test_next_chain_steps_duplicate_target_identity_does_not_block_sibling_branch(tmp_path, monkeypatch):
    # Same route as above, but this time branch1's target leg has *already*
    # been dispatched. Because the route's rolling anchor gives both
    # branches the identical target (topic, agent) too, branch1's own
    # dispatched user row would — by topic/agent/after_id alone — look like
    # branch2's target already ran (the real-world "#6130 missing its
    # child" symptom: the sibling with the higher origin id never gets its
    # own hop). The dispatched row's context.pins (server.py stores
    # step["previous_msg_ids"] there) must keep the two branches' evidence
    # apart.
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@qwen!,@qwen!>@qwen!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "qwen", "go", None),
        ("assistant", "qwen", "qwen output 1", "done"),
        ("user", "qwen", "go", None),
        ("assistant", "qwen", "qwen output 2", "done"),
    ])
    rows = stats_db.get_flow_run_messages(flow_run_id)
    asst1_id, asst2_id = sorted(r["id"] for r in rows if r["role"] == "assistant")

    # Simulate branch1's target leg already dispatched, exactly as
    # agent/flow.py's own _dispatch_next_step -> _prepare_chat_turn would:
    # pinned_ids (== previous_msg_ids) end up in the row's context.pins.
    target_user_id = stats_db.insert_user_message(
        "squid", "qwen", f"Route: {route}\nhandoff", context_ids=[asst1_id],
        source="workflow", flow_run_id=flow_run_id, flow_route=route,
    )
    target_asst_id = stats_db.insert_assistant_message("squid", "qwen", target_user_id, flow_run_id=flow_run_id, flow_route=route)
    stats_db.update_assistant_message(target_asst_id, "handoff output", None, "done")

    steps = flow.next_chain_steps(flow_run_id)
    assert len(steps) == 1
    assert steps[0]["previous_msg_ids"] == [asst2_id]


@pytest.mark.skipif(
    not TEST_HARNESS_ENABLED,
    reason="requires SQUID_TEST_HARNESS=1 in the environment before the test process starts "
           "(agent/config.py reads it once at import time)",
)
def test_continue_chain_multi_origin_via_real_echo_harness(tmp_path, monkeypatch):
    """End-to-end proof: the Squid Echo harness drives a real multi-branch
    Squid Flow chain through the actual dispatch path (topic_queue.py's
    TopicWorker, agent/flow.py's completion hook) with nothing mocked — the
    exact graph-shape bug class fixed earlier in this file (duplicate/
    misattributed origin dispatch) is now exercisable at full pipeline speed
    without a real coding-agent CLI. Run with:
        SQUID_TEST_HARNESS=1 python -m pytest tests/test_flow.py -k echo
    """
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr("agent.server.WORKTREE_ISOLATION_ENABLED", False)
    monkeypatch.setattr("agent.config.WORKTREE_ISOLATION_ENABLED", False)
    stats_db.init_db()
    stats_db.upsert_agent("echobot", "echo", "echo", None, cwd=str(tmp_path))

    route = "#squid@echobot!,@echobot!>@echobot!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "echobot", "go", None),
        ("assistant", "echobot", "origin one", "done"),
        ("user", "echobot", "go", None),
        ("assistant", "echobot", "origin two", "done"),
    ])
    rows = stats_db.get_flow_run_messages(flow_run_id)
    asst_ids = sorted(r["id"] for r in rows if r["role"] == "assistant")

    async def run():
        # Mirrors reality: each origin's own completion independently fires
        # the completion hook. A completed *target*'s own message also fires
        # continue_chain again (topic_queue.py's _trigger_chain_continuation)
        # as an un-awaited background task — so full-chain completion is
        # eventually consistent, not synchronous with these two calls; poll
        # briefly afterward instead of asserting immediately.
        # run_echo deliberately waits a real random 5-10s before replying
        # (see its docstring) — patched away here so this test stays fast;
        # that latency itself isn't what this test is checking.
        real_sleep = asyncio.sleep
        with patch("agent.runners.asyncio.sleep", new=AsyncMock()):
            for asst_id in asst_ids:
                await flow.continue_chain(asst_id)
            for _ in range(250):  # up to ~5s
                for worker in list(server.dispatcher._workers.values()):
                    await worker.q.join()
                rows = stats_db.get_flow_run_messages(flow_run_id)
                if len(rows) == 8 and all(r["status"] == "done" for r in rows if r["role"] == "assistant"):
                    break
                await real_sleep(0.02)

    asyncio.run(run())

    rows = stats_db.get_flow_run_messages(flow_run_id)
    assert len(rows) == 8  # 2 origins + 2 dispatched targets, 2 rows each
    dispatched_targets = [r for r in rows if r["role"] == "user" and r["source"] == "workflow"]
    assert len(dispatched_targets) == 2
    pinned = sorted(json.loads(r["context"])["pins"][0] for r in dispatched_targets)
    assert pinned == asst_ids  # one dispatch per origin, not both off the same one
    for user_row in dispatched_targets:
        reply = next(r for r in rows if r["role"] == "assistant" and r["reply_to"] == user_row["id"])
        assert reply["status"] == "done"
        assert reply["content"].startswith("echo: ")


def test_next_chain_steps_join_waits_for_all_origins_and_pins_both(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@a+@b>@c"
    flow_run_id, a_asst_id = _seed_chain(route, [
        ("user", "a", "compare", None),
        ("assistant", "a", "a output", "done"),
    ])
    assert flow.next_chain_steps(flow_run_id) == []

    b_user = stats_db.insert_user_message("squid", "b", "compare", flow_run_id=flow_run_id, flow_route=route)
    b_asst_id = stats_db.insert_assistant_message("squid", "b", b_user, flow_run_id=flow_run_id, flow_route=route)
    stats_db.update_assistant_message(b_asst_id, "b output", None, "done")

    steps = flow.next_chain_steps(flow_run_id)
    assert len(steps) == 1
    assert steps[0]["topic"] == "squid"
    assert steps[0]["agent"] == "c"
    assert steps[0]["previous_agent"] == "@a+@b"
    assert steps[0]["previous_msg_ids"] == [a_asst_id, b_asst_id]
    assert steps[0]["route"] == route


def test_next_chain_steps_target_join_roundtrip_dispatches_both_targets_in_parallel(tmp_path, monkeypatch):
    # The mirror image of the origin-side join test above: a round-trip's
    # target can be a join (only position besides the origin where '+' has
    # a consumer — the return leg). Topics differ here, unlike the origin
    # join test, so this doesn't use _seed_chain (hardcoded to topic "squid").
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#t1@a1<>#t2+#t3"
    flow_run_id = "f1"
    u1 = stats_db.insert_user_message("t1", "a1", "go", flow_run_id=flow_run_id, flow_route=route)
    origin_asst_id = stats_db.insert_assistant_message("t1", "a1", u1, flow_run_id=flow_run_id, flow_route=route)
    stats_db.update_assistant_message(origin_asst_id, "origin output", None, "done")

    steps = flow.next_chain_steps(flow_run_id)
    assert {(s["topic"], s["agent"]) for s in steps} == {("t2", "a1"), ("t3", "a1")}
    assert all(s["previous_msg_ids"] == [origin_asst_id] for s in steps)

    u_t2 = stats_db.insert_user_message("t2", "a1", "handoff", flow_run_id=flow_run_id, flow_route=route)
    a_t2 = stats_db.insert_assistant_message("t2", "a1", u_t2, flow_run_id=flow_run_id, flow_route=route)
    u_t3 = stats_db.insert_user_message("t3", "a1", "handoff", flow_run_id=flow_run_id, flow_route=route)
    a_t3 = stats_db.insert_assistant_message("t3", "a1", u_t3, flow_run_id=flow_run_id, flow_route=route)

    # Only one of the two joined targets has finished — no return step yet,
    # and no re-dispatch of the one already sent.
    stats_db.update_assistant_message(a_t2, "t2 output", None, "done")
    assert flow.next_chain_steps(flow_run_id) == []

    # Both finished — exactly one return step to the origin, pinning both.
    stats_db.update_assistant_message(a_t3, "t3 output", None, "done")
    steps = flow.next_chain_steps(flow_run_id)
    assert len(steps) == 1
    assert steps[0]["topic"] == "t1"
    assert steps[0]["agent"] == "a1"
    assert set(steps[0]["previous_msg_ids"]) == {a_t2, a_t3}
    assert steps[0]["route"] == route


def test_next_chain_steps_scheduled_repeat_records_delays(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex=2:1s>@review"
    flow_run_id, asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    steps = flow.next_chain_steps(flow_run_id)
    assert [(s["agent"], s["previous_msg_ids"], s["delay_seconds"]) for s in steps] == [
        ("review", [asst_id], 1),
        ("review", [asst_id], 2),
    ]


def test_next_chain_steps_repeated_roundtrip_advances_one_leg_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex<2>@review"
    flow_run_id, origin_asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    first = flow.next_chain_steps(flow_run_id)
    assert [(s["agent"], s["previous_msg_ids"]) for s in first] == [("review", [origin_asst_id])]

    review_user = stats_db.insert_user_message(
        "squid",
        "review",
        flow.chain_handoff_prompt(route, "codex", "review", False, "review this"),
        source="workflow",
        flow_run_id=flow_run_id,
        flow_route=route,
    )
    review_asst_id = stats_db.insert_assistant_message("squid", "review", review_user, flow_run_id=flow_run_id, flow_route=route)
    stats_db.update_assistant_message(review_asst_id, "review output", None, "done")

    second = flow.next_chain_steps(flow_run_id)
    assert [(s["agent"], s["previous_msg_ids"]) for s in second] == [("codex", [review_asst_id])]


def test_chain_handoff_prompt_matches_ui_template():
    prompt = flow.chain_handoff_prompt("#squid@codex>@revucla!", "codex", "revucla", True, "review this")
    assert prompt == (
        "Squid route chain handoff.\n"
        "Route: #squid@codex>@revucla!\n"
        "Previous step: @codex\n"
        "Current step: @revucla!\n"
        "Original prompt: review this\n"
        "Previous output: injected context <previous_step_output>. Use it to continue."
    )


# ---------------------------------------------------------------------------
# next_chain_step
# ---------------------------------------------------------------------------

def test_next_chain_step_one_way_sends_target_after_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex>@revucla!"
    flow_run_id, asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    step = flow.next_chain_step(flow_run_id)
    assert step == {
        "topic": "squid",
        "agent": "revucla",
        "fresh": True,
        "previous_agent": "codex",
        "previous_msg_id": asst_id,
        "original_prompt": "review this",
        "route": route,
    }


def test_next_chain_step_one_way_dispatches_target_on_its_own_topic(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex>#hive@revucla!"
    flow_run_id, asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    step = flow.next_chain_step(flow_run_id)
    assert step == {
        "topic": "hive",
        "agent": "revucla",
        "fresh": True,
        "previous_agent": "codex",
        "previous_msg_id": asst_id,
        "original_prompt": "review this",
        "route": route,
    }


def test_next_chain_step_one_way_complete_after_target(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex>@revucla!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
        ("user", "revucla", "handoff", None),
        ("assistant", "revucla", "revucla output", "done"),
    ])

    assert flow.next_chain_step(flow_run_id) is None


def test_next_chain_step_round_trip_sends_return_after_target(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex<>@revucla!"
    flow_run_id, target_asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
        ("user", "revucla", "handoff", None),
        ("assistant", "revucla", "revucla output", "done"),
    ])

    step = flow.next_chain_step(flow_run_id)
    assert step == {
        "topic": "squid",
        "agent": "codex",
        "fresh": False,
        "previous_agent": "revucla",
        "previous_msg_id": target_asst_id,
        "original_prompt": "review this",
        "route": route,
    }


def test_next_chain_step_round_trip_complete_after_return(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex<>@revucla!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
        ("user", "revucla", "handoff", None),
        ("assistant", "revucla", "revucla output", "done"),
        ("user", "codex", "return handoff", None),
        ("assistant", "codex", "codex final output", "done"),
    ])

    assert flow.next_chain_step(flow_run_id) is None


def test_next_chain_step_fail_stops_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex>@revucla!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "", "error"),
    ])

    assert flow.next_chain_step(flow_run_id) is None


def test_next_chain_step_fail_stops_on_empty_output(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex>@revucla!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "", "done"),
    ])

    assert flow.next_chain_step(flow_run_id) is None


def test_next_chain_step_ignores_unrecognized_route(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    flow_run_id, _ = _seed_chain("#squid@codex<bad>@revucla!", [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    assert flow.next_chain_step(flow_run_id) is None


# ---------------------------------------------------------------------------
# continue_chain — the actual server-side dispatch of the next step
# ---------------------------------------------------------------------------

def test_continue_chain_dispatches_target_after_origin_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    _seed_agent("codex")
    _seed_agent("revucla")
    route = "#squid@codex>@revucla!"
    flow_run_id, origin_asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    async def fake_runner(*args, **kwargs):
        yield "revucla output"

    async def run():
        with patch("agent.runners.run_codex", fake_runner):
            await flow.continue_chain(origin_asst_id)

    asyncio.run(run())

    rows = stats_db.get_flow_run_messages(flow_run_id)
    assert len(rows) == 4
    handoff, target = rows[2], rows[3]
    assert handoff["role"] == "user"
    assert handoff["agent"] == "revucla"
    assert "Squid route chain handoff." in handoff["content"]
    assert "review this" in handoff["content"]
    assert target["role"] == "assistant"
    assert target["agent"] == "revucla"
    assert target["status"] == "done"
    assert target["content"] == "revucla output"

    # Chain is now complete (one-way, 4 rows) — no further steps.
    assert flow.next_chain_step(flow_run_id) is None


def test_dispatch_or_schedule_claims_immediate_dispatch_exactly_once(tmp_path, monkeypatch):
    # Regression for a real production race: in a multi-origin chain, several
    # messages complete close enough together that more than one of them
    # calls continue_chain() -> next_chain_steps() within the same narrow
    # window, each independently computing the identical still-undispatched
    # step before either's dispatch has committed a row proving otherwise.
    # Without an atomic claim, both proceed and the step's target gets
    # dispatched twice (same content, same pinned previous_msg_ids) — the
    # observed "#6142 used as source for both #6146 and #6148" bug.
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    _seed_agent("codex")
    _seed_agent("revucla")
    route = "#squid@codex>@revucla!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])
    steps = flow.next_chain_steps(flow_run_id)
    assert len(steps) == 1
    step = steps[0]

    async def fake_runner(*args, **kwargs):
        await asyncio.sleep(0)  # yield control, mimicking real overlap
        yield "revucla output"

    async def run():
        with patch("agent.runners.run_codex", fake_runner):
            return await asyncio.gather(
                flow._dispatch_or_schedule(flow_run_id, step),
                flow._dispatch_or_schedule(flow_run_id, step),
            )

    results = asyncio.run(run())
    assert sorted(results) == [False, True]

    rows = stats_db.get_flow_run_messages(flow_run_id)
    # 2 origin rows + exactly one dispatched user/assistant pair, not two.
    assert len(rows) == 4


def test_continue_chain_dispatches_target_onto_its_own_topic(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    _seed_agent("codex")
    _seed_agent("revucla")
    route = "#squid@codex>#hive@revucla!"
    flow_run_id, origin_asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    async def fake_runner(*args, **kwargs):
        yield "revucla output"

    async def run():
        with patch("agent.runners.run_codex", fake_runner):
            await flow.continue_chain(origin_asst_id)

    asyncio.run(run())

    rows = stats_db.get_flow_run_messages(flow_run_id)
    assert len(rows) == 4
    handoff, target = rows[2], rows[3]
    assert handoff["topic"] == "hive"
    assert handoff["agent"] == "revucla"
    assert target["topic"] == "hive"
    assert target["agent"] == "revucla"
    assert target["status"] == "done"
    assert target["content"] == "revucla output"


def test_continue_chain_completes_a_round_trip_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr("agent.server.WORKTREE_ISOLATION_ENABLED", False)
    monkeypatch.setattr("agent.config.WORKTREE_ISOLATION_ENABLED", False)
    stats_db.init_db()
    _seed_agent("codex")
    _seed_agent("revucla")
    route = "#squid@codex<>@revucla!"
    flow_run_id, origin_asst_id = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    outputs = iter(["revucla output", "codex final output"])

    async def fake_runner(*args, **kwargs):
        yield next(outputs)

    async def run():
        with patch("agent.runners.run_codex", fake_runner):
            # This one call cascades the whole round trip: TopicWorker fires
            # continue_chain again itself (as a background task, same hook
            # that runs for any real completion) once the target step it
            # dispatches here finishes, sending the return-to-origin step.
            await flow.continue_chain(origin_asst_id)
            for _ in range(200):
                rows = stats_db.get_flow_run_messages(flow_run_id)
                if len(rows) >= 6 and rows[-1]["status"] != "pending":
                    break
                await asyncio.sleep(0.01)

    asyncio.run(run())

    rows = stats_db.get_flow_run_messages(flow_run_id)
    assert len(rows) == 6
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["agent"] == "codex"
    assert rows[-1]["content"] == "codex final output"
    assert flow.next_chain_step(flow_run_id) is None


def test_continue_chain_is_a_noop_for_non_chain_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    user_id = stats_db.insert_user_message("squid", "codex", "just a normal message")
    asst_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(asst_id, "codex output", None, "done")

    asyncio.run(flow.continue_chain(asst_id))  # must not raise, must not touch anything

    row = stats_db.get_message(asst_id)
    assert row["content"] == "codex output"


# ---------------------------------------------------------------------------
# sweep_incomplete_flows — boot-time recovery
# ---------------------------------------------------------------------------

def test_sweep_resumes_a_stalled_chain(tmp_path, monkeypatch):
    """Reproduces the real-world bug this module fixes: a chain step finished
    but the next one was never dispatched (e.g. the client that used to drive
    continuation refreshed mid-chain). The sweep should pick it back up."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    _seed_agent("codex")
    _seed_agent("revucla")
    route = "#squid@codex>@revucla!"
    flow_run_id, _ = _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
    ])

    async def fake_runner(*args, **kwargs):
        yield "revucla output"

    async def run():
        with patch("agent.runners.run_codex", fake_runner):
            return await flow.sweep_incomplete_flows()

    resumed = asyncio.run(run())

    assert resumed == 1
    rows = stats_db.get_flow_run_messages(flow_run_id)
    assert len(rows) == 4
    assert rows[-1]["status"] == "done"
    assert rows[-1]["content"] == "revucla output"


def test_sweep_is_a_noop_when_nothing_is_stalled(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    route = "#squid@codex>@revucla!"
    _seed_chain(route, [
        ("user", "codex", "review this", None),
        ("assistant", "codex", "codex output", "done"),
        ("user", "revucla", "handoff", None),
        ("assistant", "revucla", "revucla output", "done"),
    ])

    assert asyncio.run(flow.sweep_incomplete_flows()) == 0
