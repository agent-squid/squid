import asyncio
from unittest.mock import patch

from agent import flow
from agent import stats_db


def _seed_agent(name):
    stats_db.upsert_agent(name, "codex", None, None, cwd="/tmp/project")


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
    # <N> numbered rounds are not implemented yet (ADR-0032) — must not be
    # misread as a plain chain.
    assert flow.parse_route_chain("#squid@codex<2>@revucla") is None


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
    flow_run_id, _ = _seed_chain("#squid@codex<2>@revucla!", [
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
