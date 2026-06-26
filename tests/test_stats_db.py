import sqlite3

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


def test_recent_prompts_keep_default_adhoc_prompts_plain(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("default", None, "plain prompt")
    stats_db.insert_assistant_message("default", None, user_id, adhoc=True)

    assert stats_db.get_recent_prompts(limit=5) == ["plain prompt"]


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


def test_mark_orphaned_pending_preserves_contentful_assistant_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    content_user_id = stats_db.insert_user_message("squid", "codex", "contentful")
    content_asst_id = stats_db.insert_assistant_message("squid", "codex", content_user_id, adhoc=False)
    stats_db.update_assistant_message(content_asst_id, "recovered content", "session-1", "pending")

    empty_user_id = stats_db.insert_user_message("squid", "codex", "empty")
    empty_asst_id = stats_db.insert_assistant_message("squid", "codex", empty_user_id, adhoc=False)

    assert stats_db.mark_orphaned_pending() == 2
    assert stats_db.get_message(content_asst_id)["status"] == "done"
    assert stats_db.get_message(empty_asst_id)["status"] == "error"


def test_init_db_migrates_existing_chat_messages_turn_index(tmp_path, monkeypatch):
    db_path = tmp_path / "squid.db"
    monkeypatch.setattr(stats_db, "_DB_PATH", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL DEFAULT 'default',
                agent TEXT,
                session_id TEXT,
                role TEXT NOT NULL,
                content TEXT,
                reply_to INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                adhoc INTEGER DEFAULT 0,
                context TEXT,
                quota_delta REAL,
                quota_before REAL,
                quota_after REAL,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )"""
        )
        conn.execute("INSERT INTO chat_messages (topic, agent, role, content, status) VALUES ('squid', 'codex', 'user', 'first', 'done')")
        conn.execute("INSERT INTO chat_messages (topic, agent, role, content, status, reply_to, session_id, adhoc) VALUES ('squid', 'codex', 'assistant', 'one', 'done', 1, 'session-1', 0)")
        conn.execute("INSERT INTO chat_messages (topic, agent, role, content, status) VALUES ('squid', 'codex', 'user', 'adhoc', 'done')")
        conn.execute("INSERT INTO chat_messages (topic, agent, role, content, status, reply_to, session_id, adhoc) VALUES ('squid', 'codex', 'assistant', 'adhoc', 'done', 3, 'adhoc-session', 1)")
        conn.execute("INSERT INTO chat_messages (topic, agent, role, content, status) VALUES ('squid', 'codex', 'user', 'second', 'done')")
        conn.execute("INSERT INTO chat_messages (topic, agent, role, content, status, reply_to, session_id, adhoc) VALUES ('squid', 'codex', 'assistant', 'two', 'done', 5, 'session-1', 0)")

    stats_db.init_db()

    rows = stats_db.get_messages_flat(topic="squid", agent="codex", limit=10)["items"]
    by_prompt = {row["prompt"]: row for row in rows}
    assert by_prompt["first"]["session_turn_count"] == 1
    assert by_prompt["second"]["session_turn_count"] == 2
    assert by_prompt["adhoc"]["session_turn_count"] is None


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
