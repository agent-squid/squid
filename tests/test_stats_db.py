from agent import stats_db


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
