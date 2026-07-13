import json
import sqlite3
from agent import stats_db


def test_init_db_seeds_pi_agent_when_pi_harness_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr(stats_db, "SUPPORTED_HARNESSES", frozenset({"pi"}))
    monkeypatch.setattr(stats_db, "is_installed", lambda harness: harness == "pi")

    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        row = conn.execute(
            "SELECT name, harness, provider, model, cwd FROM agents WHERE name = 'pi'"
        ).fetchone()

    assert row == ("pi", "pi", "nvidia", "deepseek-ai/deepseek-v4-pro", None)


def test_init_db_seeds_claude_agent_without_claudecode_or_haiku_names(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr(stats_db, "SUPPORTED_HARNESSES", frozenset({"claudecode"}))
    monkeypatch.setattr(stats_db, "is_installed", lambda harness: harness == "claudecode")

    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        rows = conn.execute(
            "SELECT name, harness, provider, model FROM agents ORDER BY name"
        ).fetchall()

    assert rows == [("claude", "claudecode", "anthropic", None)]


def test_init_db_seeds_five_default_agents_when_all_harnesses_are_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr(
        stats_db,
        "SUPPORTED_HARNESSES",
        frozenset({"claudecode", "codex", "cursor", "opencode", "pi"}),
    )
    monkeypatch.setattr(stats_db, "is_installed", lambda harness: True)

    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        rows = conn.execute("SELECT name FROM agents ORDER BY name").fetchall()

    assert [row[0] for row in rows] == ["claude", "codex", "cursor", "opencode", "pi"]


def test_aggregated_stats_includes_requested_chart_percentile(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.executemany(
            """INSERT INTO session_stats(
                   session_id, topic, agent, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, created_at
               ) VALUES (?, 'squid', 'codex', ?, 0, 0, 0, 0, ?)""",
            [
                ("s1", 10, "2026-07-10T10:00:00Z"),
                ("s2", 20, "2026-07-10T10:10:00Z"),
                ("s3", 30, "2026-07-10T10:20:00Z"),
            ],
        )

    rows = stats_db.get_aggregated_stats(
        period="daily",
        days=0,
        chart_series=[{"metric": "tokens_in", "agg": "p50"}],
    )

    assert rows[0]["chart_tokens_in_p50"] == 20


def test_aggregated_stats_cutoff_ignores_created_at_string_format(tmp_path, monkeypatch):
    """session_stats.created_at is written via SQLite's CURRENT_TIMESTAMP
    ('YYYY-MM-DD HH:MM:SS'), not the 'YYYY-MM-DDTHH:MM:SSZ' format _stats_cutoff
    produces. Comparing them as raw strings sorts ' ' before 'T', so any row on
    the same calendar day as the cutoff was silently dropped regardless of its
    actual time of day (this is why selecting "1 day" showed only a few hours)
    — the days filter must compare via datetime(), not lexicographically."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr(stats_db, "_stats_cutoff", lambda days: "2026-07-12T05:36:00Z")
    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.executemany(
            """INSERT INTO session_stats(
                   session_id, topic, agent, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, created_at
               ) VALUES (?, 'squid', 'codex', ?, 0, 0, 0, 0, ?)""",
            [
                # Same calendar day as the cutoff, but later in the day — must be included.
                ("late-same-day", 100, "2026-07-12 23:59:00"),
                # Entirely before the cutoff's day — must be excluded.
                ("before-cutoff", 999, "2026-07-11 23:59:00"),
            ],
        )

    rows = stats_db.get_aggregated_stats(period="daily", days=1)

    total_in = sum(r.get("input_tokens") or 0 for r in rows)
    assert total_in == 100


def test_breakdown_stats_includes_requested_chart_percentile(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.executemany(
            """INSERT INTO session_stats(
                   session_id, topic, agent, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, created_at
               ) VALUES (?, 'squid', ?, ?, 0, 0, 0, 0, ?)""",
            [
                ("s1", "codex", 10, "2026-07-10T10:00:00Z"),
                ("s2", "codex", 20, "2026-07-10T10:10:00Z"),
                ("s3", "codex", 30, "2026-07-10T10:20:00Z"),
                ("s4", "claude", 100, "2026-07-10T10:20:00Z"),
            ],
        )

    rows = stats_db.get_stats_by_breakdown(
        period="daily",
        days=0,
        breakdown="agent",
        chart_series=[{"metric": "tokens_in", "agg": "p50"}],
    )

    by_agent = {row["agent"]: row for row in rows}
    assert by_agent["codex"]["chart_tokens_in_p50"] == 20
    assert by_agent["claude"]["chart_tokens_in_p50"] == 100



def test_topic_agent_history_uses_mode_specific_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.upsert_topic("squid", "codex", last_prompt="session prompt", adhoc=False)
    stats_db.upsert_topic("squid", "codex", last_prompt="adhoc prompt", adhoc=True)

    rows = stats_db.get_topic_agent_history("squid")

    assert len(rows) == 1
    assert rows[0]["agent"] == "codex"
    assert rows[0]["last_prompt"] == "session prompt"
    assert rows[0]["last_adhoc_prompt"] == "adhoc prompt"


def test_topics_management_summary_includes_hidden_and_agent_lanes(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.upsert_topic("squid", "codex", last_prompt="session prompt", adhoc=False)
    stats_db.upsert_topic("squid", "codex", last_prompt="adhoc prompt", adhoc=True)
    stats_db.set_topic_hidden("squid", True)

    visible = stats_db.get_topics_management_summary(include_hidden=False)
    all_topics = stats_db.get_topics_management_summary(include_hidden=True)

    assert visible == []
    assert len(all_topics) == 1
    assert all_topics[0]["name"] == "squid"
    assert all_topics[0]["hidden"] is True
    assert all_topics[0]["agent"] == "codex"
    assert all_topics[0]["agents"] == [{
        "agent": "codex",
        "last_prompt": "session prompt",
        "last_adhoc_prompt": "adhoc prompt",
        "last_at": all_topics[0]["agents"][0]["last_at"],
        "last_model": None,
        "last_harness": None,
        "last_provider": None,
        "last_backend": None,
        "session_turns": 0,
        "adhoc_turns": 0,
        "agent_turns": 0,
        "live_turns": 0,
    }]


def test_recent_prompts_returns_limit_unique_routed_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    for _ in range(10):
        user_id = stats_db.insert_user_message("squid", "haiku", "push the changes")
        stats_db.insert_assistant_message("squid", "haiku", user_id, adhoc=True)

    for i in range(5):
        user_id = stats_db.insert_user_message("topic", "codex", f"unique prompt {i}")
        stats_db.insert_assistant_message("topic", "codex", user_id, adhoc=False)

    prompts = stats_db.get_recent_prompts(limit=5)

    assert prompts == [
        "#topic@codex unique prompt 4",
        "#topic@codex unique prompt 3",
        "#topic@codex unique prompt 2",
        "#topic@codex unique prompt 1",
        "#topic@codex unique prompt 0",
    ]
    assert stats_db.get_recent_prompts(limit=6)[-1] == "#squid@haiku! push the changes"


def test_recent_prompts_keep_default_adhoc_prompts_plain(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("default", None, "plain prompt")
    stats_db.insert_assistant_message("default", None, user_id, adhoc=True)

    assert stats_db.get_recent_prompts(limit=5) == ["plain prompt"]


def test_recent_prompts_keep_default_agent_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("default", "opencode", "what's your model")
    stats_db.insert_assistant_message("default", "opencode", user_id, adhoc=True)

    assert stats_db.get_recent_prompts(limit=5) == ["#default@opencode! what's your model"]


def test_recent_prompts_normalize_adhoc_lookback(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    old_user_id = stats_db.insert_user_message("squid", "codex", "reuse context", lookback=2)
    stats_db.insert_assistant_message("squid", "codex", old_user_id, adhoc=True)
    new_user_id = stats_db.insert_user_message("squid", "codex", "reuse context", lookback=0)
    stats_db.insert_assistant_message("squid", "codex", new_user_id, adhoc=True)

    assert stats_db.get_recent_prompts(limit=5) == ["#squid@codex! reuse context"]


def test_session_injected_context_recovers_pins_and_memory_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message(
        "squid",
        "codex",
        "use context",
        context_ids=[7, 7, 8],
        mem=True,
        mem_revision="rev-1",
    )
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(assistant_id, "done", "session-1", "done")

    context = stats_db.get_session_injected_context("session-1")

    assert context == {
        "injected_ids": [7, 8],
        "memory_injected": True,
        "memory_revision": "rev-1",
    }


def test_session_injected_context_recovers_legacy_memory_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "use memory", mem=True)
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(assistant_id, "done", "session-1", "done")

    context = stats_db.get_session_injected_context("session-1")

    assert context == {
        "injected_ids": [],
        "memory_injected": True,
        "memory_revision": None,
    }


def test_history_items_include_session_turn_count(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    old_user_id = stats_db.insert_user_message("squid", "codex", "old")
    old_asst_id = stats_db.insert_assistant_message("squid", "codex", old_user_id, adhoc=False)
    stats_db.update_assistant_message(old_asst_id, "old response", "session-old", "done")

    first_user_id = stats_db.insert_user_message("squid", "codex", "first")
    first_asst_id = stats_db.insert_assistant_message("squid", "codex", first_user_id, adhoc=False)
    stats_db.update_assistant_message(first_asst_id, "first response", "session-1", "done")

    adhoc_user_id = stats_db.insert_user_message("squid", "codex", "adhoc")
    adhoc_asst_id = stats_db.insert_assistant_message("squid", "codex", adhoc_user_id, adhoc=True)
    stats_db.update_assistant_message(adhoc_asst_id, "adhoc response", None, "done")

    second_user_id = stats_db.insert_user_message("squid", "codex", "second")
    second_asst_id = stats_db.insert_assistant_message("squid", "codex", second_user_id, adhoc=False)
    stats_db.update_assistant_message(second_asst_id, "second response", "session-1", "done")

    pending_user_id = stats_db.insert_user_message("squid", "codex", "pending")
    pending_asst_id = stats_db.insert_assistant_message("squid", "codex", pending_user_id, adhoc=False)

    history = stats_db.get_messages_flat(topic="squid", agent="codex", limit=10)["items"]

    by_prompt = {item["prompt"]: item for item in history}
    assert by_prompt["old"]["session_turn_count"] == 1
    assert by_prompt["first"]["session_turn_count"] == 1
    assert by_prompt["adhoc"]["session_turn_count"] is None
    assert by_prompt["second"]["session_turn_count"] == 2
    assert by_prompt["pending"]["session_turn_count"] is None
    assert stats_db.ensure_session_turn_index(pending_asst_id, "session-1") == 3
    assert stats_db.get_message(pending_asst_id)["session_turn_count"] == 3


def test_mark_orphaned_pending_recovers_only_completed_run_text(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    content_user_id = stats_db.insert_user_message("squid", "codex", "contentful")
    content_asst_id = stats_db.insert_assistant_message("squid", "codex", content_user_id, adhoc=False)
    stats_db.update_assistant_message(content_asst_id, "partial snapshot", "session-1", "pending")
    stats_db.insert_run_event(content_asst_id, 0, "status", "Working...")
    stats_db.insert_run_event(content_asst_id, 1, "text", "recovered ")
    stats_db.insert_run_event(content_asst_id, 2, "text", "content")
    stats_db.insert_run_event(content_asst_id, 3, "done", None)

    empty_user_id = stats_db.insert_user_message("squid", "codex", "empty")
    empty_asst_id = stats_db.insert_assistant_message("squid", "codex", empty_user_id, adhoc=False)
    stats_db.update_assistant_message(empty_asst_id, "partial snapshot", "session-1", "pending")
    stats_db.insert_run_event(empty_asst_id, 0, "status", "Working...")
    stats_db.insert_run_event(empty_asst_id, 1, "done", None)

    assert stats_db.mark_orphaned_pending() == 2
    content_row = stats_db.get_message(content_asst_id)
    assert content_row["status"] == "done"
    assert content_row["content"] == "recovered content"
    empty_row = stats_db.get_message(empty_asst_id)
    assert empty_row["status"] == "error"
    assert empty_row["content"] == ""


def test_mark_orphaned_pending_respects_created_at_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    before_user_id = stats_db.insert_user_message("squid", "codex", "before")
    before_asst_id = stats_db.insert_assistant_message("squid", "codex", before_user_id, adhoc=False)
    after_user_id = stats_db.insert_user_message("squid", "codex", "after")
    after_asst_id = stats_db.insert_assistant_message("squid", "codex", after_user_id, adhoc=False)

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute("UPDATE chat_messages SET created_at=? WHERE id=?", ("2026-07-09T19:00:00Z", before_asst_id))
        conn.execute("UPDATE chat_messages SET created_at=? WHERE id=?", ("2026-07-09T19:30:00Z", after_asst_id))

    assert stats_db.mark_orphaned_pending(before_created_at="2026-07-09T19:15:00Z") == 1
    assert stats_db.get_message(before_asst_id)["status"] == "error"
    assert stats_db.get_message(after_asst_id)["status"] == "pending"


def test_grouped_stats_include_quota_delta(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.save_stats(
        "session-1",
        {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25},
        topic="squid",
        agent="codex",
    )
    stats_db.save_quota_delta("session-1", 40.0, 42.5)
    stats_db.save_stats(
        "session-2",
        {"input_tokens": 20, "output_tokens": 10, "cost_usd": 0.50},
        topic="squid",
        agent="codex",
    )
    stats_db.save_quota_delta("session-2", 42.5, 43.0)

    by_topic = stats_db.get_stats_by_topic(days=0)
    by_agent = stats_db.get_stats_by_agent(days=0)

    assert by_topic[0]["cost_usd"] == 0.75
    assert by_topic[0]["quota_delta"] == 3.0
    assert by_agent[0]["cost_usd"] == 0.75
    assert by_agent[0]["quota_delta"] == 3.0


def test_get_stats_by_turn_uses_per_turn_stats_not_stale_session_row(tmp_path, monkeypatch):
    """A resumed session shares one session_stats row across turns, so session_stats
    only ever holds the *latest* turn's numbers. get_stats_by_turn must instead read
    each turn's own duration/tokens/cost from its run_events "stats" snapshot."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user1 = stats_db.insert_user_message("squid", "codex", "first")
    asst1 = stats_db.insert_assistant_message("squid", "codex", user1, adhoc=False)
    stats_db.update_assistant_message(asst1, "first response", "session-1", "done")
    stats_db.insert_run_event(asst1, 0, "stats", json.dumps(
        {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.1, "duration_ms": 5000}
    ))
    stats_db.update_message_quota_snapshot(asst1, 40.0, 41.0)

    user2 = stats_db.insert_user_message("squid", "codex", "second")
    asst2 = stats_db.insert_assistant_message("squid", "codex", user2, adhoc=False)
    stats_db.update_assistant_message(asst2, "second response", "session-1", "done")
    # Same session_id resumed for turn 2 — an interim stats event followed by the final one.
    stats_db.insert_run_event(asst2, 0, "stats", json.dumps(
        {"input_tokens": 150, "output_tokens": 5, "cost_usd": 0.05, "duration_ms": 1000}
    ))
    stats_db.insert_run_event(asst2, 1, "stats", json.dumps(
        {"input_tokens": 200, "output_tokens": 20, "cost_usd": 0.2, "duration_ms": 8000}
    ))
    stats_db.update_message_quota_snapshot(asst2, 41.0, 41.5)

    rows = stats_db.get_stats_by_turn(days=0)
    by_msg = {r["msg_id"]: r for r in rows}

    assert len(rows) == 2
    assert by_msg[asst1]["duration_ms"] == 5000
    assert by_msg[asst1]["cost_usd"] == 0.1
    assert by_msg[asst1]["quota_delta"] == 1.0
    # Turn 2 keeps its own (later, larger) numbers — not turn 1's, and not the
    # interim stats event, only the final one for that turn.
    assert by_msg[asst2]["duration_ms"] == 8000
    assert by_msg[asst2]["input_tokens"] == 200
    assert by_msg[asst2]["quota_delta"] == 0.5
    # Newest turn first.
    assert [r["msg_id"] for r in rows] == [asst2, asst1]


def test_get_stats_by_turn_filters_by_agent_topic_and_adhoc(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    codex_user = stats_db.insert_user_message("squid", "codex", "codex turn")
    codex_asst = stats_db.insert_assistant_message("squid", "codex", codex_user, adhoc=False)
    stats_db.update_assistant_message(codex_asst, "ok", "session-codex", "done")
    stats_db.insert_run_event(codex_asst, 0, "stats", json.dumps({"duration_ms": 111}))

    other_user = stats_db.insert_user_message("other-topic", "claude", "claude turn")
    other_asst = stats_db.insert_assistant_message("other-topic", "claude", other_user, adhoc=True)
    stats_db.update_assistant_message(other_asst, "ok", "session-claude", "done")
    stats_db.insert_run_event(other_asst, 0, "stats", json.dumps({"duration_ms": 222}))

    assert [r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, agent="codex")] == [codex_asst]
    assert [r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, topic="other-topic")] == [other_asst]
    assert [r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, adhoc="adhoc")] == [other_asst]
    assert [r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, adhoc="session")] == [codex_asst]


def test_agent_session_breakdown_keeps_agent_session_variants_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.save_stats(
        "session-1",
        {"input_tokens": 100, "output_tokens": 10, "cost_usd": 1.0},
        topic="squid",
        agent="codex",
        backend="codex",
        model="gpt-5",
        cwd="/repo",
    )
    stats_db.save_stats(
        "session-2",
        {"input_tokens": 40, "output_tokens": 8, "cost_usd": 0.4, "adhoc": True},
        topic="squid",
        agent="codex",
        backend="codex",
        model="gpt-5",
        cwd="/repo",
    )

    rows = stats_db.get_stats_by_agent_breakdown(days=0, period="daily", include_session=True)
    by_agent = {row["agent"]: row for row in rows}

    assert by_agent["codex"]["input_tokens"] == 100
    assert by_agent["codex!"]["input_tokens"] == 40
    assert by_agent["codex"]["agent_key"] != by_agent["codex!"]["agent_key"]


def test_agent_session_breakdown_includes_message_only_adhoc_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.save_stats(
        "session-1",
        {"input_tokens": 100, "output_tokens": 10, "cost_usd": 1.0},
        topic="squid",
        agent="opencode",
        backend="opencode",
        model="gpt-5",
        cwd="/repo",
    )
    for prompt in ("one", "two"):
        user_id = stats_db.insert_user_message("squid", "opencode", prompt)
        assistant_id = stats_db.insert_assistant_message("squid", "opencode", user_id, adhoc=True)
        stats_db.update_assistant_message(assistant_id, "done", None, "done")

    rows = stats_db.get_stats_by_agent_breakdown(
        days=0,
        period="daily",
        agent="opencode",
        include_session=True,
    )
    by_agent = {row["agent"]: row for row in rows}

    assert by_agent["opencode"]["sessions"] == 1
    assert by_agent["opencode!"]["sessions"] == 0
    assert by_agent["opencode!"]["total_turns"] == 2


def test_agent_breakdown_groups_by_agent_label(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.save_stats(
        "session-1",
        {"input_tokens": 100, "output_tokens": 10, "cost_usd": 1.0},
        topic="squid",
        agent="codex",
        backend="codex",
        model="gpt-5",
        cwd="/repo",
    )
    stats_db.save_stats(
        "session-2",
        {"input_tokens": 40, "output_tokens": 8, "cost_usd": 0.4},
        topic="squid",
        agent="codex",
        backend="codex",
        model="gpt-5",
        cwd="/repo",
    )

    rows = stats_db.get_stats_by_agent_breakdown(days=0, period="daily")
    by_agent = {row["agent"]: row for row in rows}

    assert list(by_agent) == ["codex"]
    assert by_agent["codex"]["input_tokens"] == 140


# ── status_raw persistence ───────────────────────────────────────────────────


def test_status_raw_persisted_and_retrieved(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(
        assistant_id, "response text", "session-1", "done",
        status_raw="Working...\nAnalyzing files\nComplete",
    )

    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] == "Working...\nAnalyzing files\nComplete"


def test_status_raw_included_in_history_and_search(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test prompt")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(
        assistant_id, "searchable response", "session-1", "done",
        status_raw="Working...\nDone",
    )

    history = stats_db.get_messages_flat(topic="squid", agent="codex")
    assert history["items"][0]["status_raw"] == "Working...\nDone"

    search = stats_db.search_messages("searchable", topic="squid", agent="codex")
    assert search["items"][0]["status_raw"] == "Working...\nDone"


def test_reverting_later_gitdiff_unblocks_older_same_file(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    repo = "/tmp/project"
    first_user_id = stats_db.insert_user_message("squid", "codex", "first")
    first_id = stats_db.insert_assistant_message("squid", "codex", first_user_id, adhoc=False)
    first_context = json.dumps([{
        "name": "GitDiff",
        "repo": repo,
        "files": [{"status": "M", "path": "app.txt"}],
        "diff": "diff --git a/app.txt b/app.txt\n",
    }])
    stats_db.update_assistant_message(first_id, "first", "session-1", "done", context=first_context)

    second_user_id = stats_db.insert_user_message("squid", "codex", "second")
    second_id = stats_db.insert_assistant_message("squid", "codex", second_user_id, adhoc=False)
    second_context = json.dumps([{
        "name": "GitDiff",
        "repo": repo,
        "files": [{"status": "M", "path": "app.txt"}],
        "diff": "diff --git a/app.txt b/app.txt\n",
    }])
    stats_db.update_assistant_message(second_id, "second", "session-1", "done", context=second_context)

    assert stats_db.get_diff_revert_eligibility(first_id, repo) == {"app.txt": "conflicting"}

    stats_db.record_git_diff_revert(second_id, repo, ["app.txt"])

    assert stats_db.get_diff_revert_eligibility(first_id, repo) == {"app.txt": "revertable"}


def test_get_messages_by_ids_includes_compact_gitdiff_context(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "implement change")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    context = json.dumps([{
        "name": "GitDiff",
        "repo": "/tmp/project",
        "source": "/tmp/project",
        "worktree_repo": "/tmp/.squid/worktrees/abcd1234/sqd-squid-1234-deadbe",
        "file_count": 2,
        "additions": 12,
        "deletions": 3,
        "stat": " app.py | 10 +++++++---\n test_app.py | 5 +++++",
        "files": [
            {"status": "M", "path": "app.py"},
            {"status": "A", "path": "test_app.py"},
        ],
        "diff": "full diff should not be injected",
    }])
    stats_db.update_assistant_message(
        assistant_id, "implemented", "session-1", "done", context=context,
    )

    messages = stats_db.get_messages_by_ids([assistant_id])

    assert messages == [
        {"role": "user", "content": "implement change"},
        {"role": "assistant", "content": (
            "implemented\n\n"
            "Changed files from this response:\n"
            "<changed_files>\n"
            "Repo: /tmp/project\n"
            "Summary: 2 files, +12 -3\n"
            "Stat:\n"
            "app.py | 10 +++++++---\n"
            " test_app.py | 5 +++++\n"
            "Files:\n"
            "- M app.py\n"
            "- A test_app.py\n"
            "</changed_files>"
        )},
    ]
    assert "full diff should not be injected" not in messages[1]["content"]
    assert "sqd-squid-1234-deadbe" not in messages[1]["content"]


def test_get_messages_by_ids_sanitizes_transient_worktree_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "review old change")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    context = json.dumps([{
        "name": "GitDiff",
        "repo": "/Users/alice/Work/squid",
        "worktree_repo": "/Users/alice/.squid/worktrees/abcd1234/sqd-squid-2066-921e61",
        "file_count": 1,
        "additions": 1,
        "deletions": 0,
        "stat": " agent/stats_db.py | 1 +",
        "files": [{"status": "M", "path": "agent/stats_db.py"}],
        "diff": "full diff should not be injected",
    }])
    old_body = (
        "I'll implement this in "
        "`/Users/alice/.squid/worktrees/abcd1234/sqd-squid-2066-921e61`.\n\n"
        "Changed files from this response:\n"
        "<changed_files>\n"
        "Repo: /Users/alice/.squid/worktrees/abcd1234/sqd-squid-2066-921e61\n"
        "Summary: 1 file, +1 -0\n"
        "Files:\n"
        "- M agent/stats_db.py\n"
        "</changed_files>"
    )
    stats_db.update_assistant_message(
        assistant_id, old_body, "session-1", "done", context=context,
    )

    messages = stats_db.get_messages_by_ids([assistant_id])
    assistant_content = messages[1]["content"]

    assert "/Users/alice/.squid/worktrees" not in assistant_content
    assert "sqd-squid-2066-921e61" not in assistant_content
    assert "[temporary Squid worktree]" in assistant_content
    assert assistant_content.count("Changed files from this response:") == 1
    assert "Repo: /Users/alice/Work/squid" in assistant_content
    assert "Repo: [temporary Squid worktree]" not in assistant_content
    assert "full diff should not be injected" not in assistant_content


def test_search_messages_can_filter_bookmarks_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    bookmarked_user_id = stats_db.insert_user_message("squid", "codex", "bookmarked prompt")
    bookmarked_id = stats_db.insert_assistant_message("squid", "codex", bookmarked_user_id, adhoc=False)
    stats_db.update_assistant_message(bookmarked_id, "needle bookmarked response", "session-1", "done")
    stats_db.add_bookmark(bookmarked_id, "squid", "codex", "needle bookmarked response")

    newer_user_id = stats_db.insert_user_message("squid", "codex", "newer prompt")
    newer_id = stats_db.insert_assistant_message("squid", "codex", newer_user_id, adhoc=False)
    stats_db.update_assistant_message(newer_id, "needle unbookmarked response", "session-2", "done")

    search = stats_db.search_messages("needle", topic="squid", agent="codex", bookmarked=True, limit=1)

    assert [item["id"] for item in search["items"]] == [bookmarked_id]


def test_search_prompts_uses_prompt_fts_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    prompt_match_user_id = stats_db.insert_user_message("squid", "codex", "needle prompt")
    prompt_match_id = stats_db.insert_assistant_message("squid", "codex", prompt_match_user_id, adhoc=False)
    stats_db.update_assistant_message(prompt_match_id, "ordinary response", "session-1", "done")

    content_match_user_id = stats_db.insert_user_message("squid", "codex", "ordinary prompt")
    content_match_id = stats_db.insert_assistant_message("squid", "codex", content_match_user_id, adhoc=False)
    stats_db.update_assistant_message(content_match_id, "needle response", "session-2", "done")

    search = stats_db.search_prompts("needle", topic="squid", agent="codex", limit=1)

    assert [item["id"] for item in search["items"]] == [prompt_match_id]


def test_status_raw_preserved_across_partial_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)

    # Simulate periodic save during streaming
    stats_db.update_assistant_message(
        assistant_id, "partial", "session-1", "pending",
        status_raw="Working...\nThinking",
    )
    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] == "Working...\nThinking"

    # Final save with more status
    stats_db.update_assistant_message(
        assistant_id, "full response", "session-1", "done",
        status_raw="Working...\nThinking\nDone",
    )
    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] == "Working...\nThinking\nDone"


def test_status_raw_null_preserved(tmp_path, monkeypatch):
    """Null status_raw shouldn't crash — old callers may not pass it."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(assistant_id, "response", "session-1", "done")

    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] is None


def test_status_raw_survives_only_if_pending_guard(tmp_path, monkeypatch):
    """only_if_pending=True should write status_raw only when status is pending."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)

    # First call: pending → updates
    stats_db.update_assistant_message(
        assistant_id, "content", "session-1", "done",
        status_raw="Streaming status",
        only_if_pending=True,
    )
    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] == "Streaming status"

    # Second call: already done → ignored
    stats_db.update_assistant_message(
        assistant_id, "new content", "session-1", "done",
        status_raw="Should not update",
        only_if_pending=True,
    )
    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] == "Streaming status"  # unchanged


def test_insert_run_event_seq_collision_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    import logging
    logger = logging.getLogger("squid")
    logger.propagate = True

    # Insert at same (msg_id, seq) twice — second should log warning
    stats_db.insert_run_event(1, 0, "text", "first")
    assert "run_event seq collision" not in caplog.text

    stats_db.insert_run_event(1, 0, "status", "collides")
    assert "run_event seq collision" in caplog.text
    assert "collides" in caplog.text or "status" in caplog.text


def test_insert_run_event_different_seq_no_collision(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    import logging
    logger = logging.getLogger("squid")
    logger.propagate = True

    stats_db.insert_run_event(1, 0, "status", "Working...")
    stats_db.insert_run_event(1, 1, "text", "hello")
    stats_db.insert_run_event(1, 2, "tool", '{"name":"Read"}')
    stats_db.insert_run_event(1, 3, "done", None)

    assert "run_event seq collision" not in caplog.text

    events = stats_db.get_run_events(1)
    assert len(events) == 4
    assert events[0]["event_type"] == "status"
    assert events[1]["event_type"] == "text"
    assert events[2]["event_type"] == "tool"
    assert events[3]["event_type"] == "done"


def test_run_event_seq_collision_different_msg_id_allowed(tmp_path, monkeypatch, caplog):
    """Same seq on different msg_id is fine — only (msg_id, seq) is unique."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    import logging
    logger = logging.getLogger("squid")
    logger.propagate = True

    stats_db.insert_run_event(1, 0, "status", "msg1")
    stats_db.insert_run_event(2, 0, "status", "msg2")

    assert "run_event seq collision" not in caplog.text


def test_mark_worktree_synced_updates_status_and_last_used(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.save_worktree("t", "901", "/repo", "/repo-wt", "sqd-t-901")
    before = stats_db.get_worktrees("t", "901")[0]
    assert before["status"] == "active"

    stats_db.mark_worktree_synced("t", "901", "/repo")

    after = stats_db.get_worktrees("t", "901")[0]
    assert after["status"] == "synced"
    assert after["last_used_at"] >= before["last_used_at"]
