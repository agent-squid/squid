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
