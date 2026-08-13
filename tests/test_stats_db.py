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

    assert rows == [
        ("clarev", "claudecode", "anthropic", None),
        ("claude", "claudecode", "anthropic", None),
    ]


def test_chat_messages_source_defaults_and_can_mark_system(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    human_user = stats_db.insert_user_message("squid", "codex", "human prompt")
    system_user = stats_db.insert_user_message("squid", "codex", "handoff prompt", source="workflow")
    system_asst = stats_db.insert_assistant_message("squid", "codex", system_user)

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        rows = conn.execute(
            "SELECT id, source FROM chat_messages WHERE role='user' ORDER BY id"
        ).fetchall()

    assert rows == [(human_user, "human"), (system_user, "workflow")]
    assert stats_db.get_message(system_asst)["prompt_source"] == "workflow"


def test_get_session_turn_boundaries_orders_by_turn_and_converts_to_epoch_ms(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    u1 = stats_db.insert_user_message("squid", "opencode", "first")
    m1 = stats_db.insert_assistant_message("squid", "opencode", u1)
    stats_db.update_assistant_message(m1, "reply one", "ses_abc", "done")

    u2 = stats_db.insert_user_message("squid", "opencode", "second")
    m2 = stats_db.insert_assistant_message("squid", "opencode", u2)
    stats_db.update_assistant_message(m2, "reply two", "ses_abc", "done")

    # A different session's turns must never leak in.
    u3 = stats_db.insert_user_message("squid", "opencode", "other session")
    m3 = stats_db.insert_assistant_message("squid", "opencode", u3)
    stats_db.update_assistant_message(m3, "reply", "ses_other", "done")

    turns = stats_db.get_session_turn_boundaries("ses_abc")

    assert [t["msg_id"] for t in turns] == [m1, m2]
    assert [t["turn_index"] for t in turns] == [1, 2]
    assert all(isinstance(t["time_ms"], int) and t["time_ms"] > 0 for t in turns)


def test_get_session_turn_boundaries_empty_for_unknown_session(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()
    assert stats_db.get_session_turn_boundaries("ses_does_not_exist") == []


def test_message_annotations_store_bad_response_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "fix it")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(msg_id, "partial fix", None)

    stats_db.set_message_annotation(
        msg_id,
        "bad_response",
        {"reasons": ["incomplete"]},
    )

    annotation = stats_db.get_message_annotation(msg_id, "bad_response")
    assert json.loads(annotation["payload"]) == {"reasons": ["incomplete"]}
    assert stats_db.get_message(msg_id)["marked_bad"] == 1


def test_init_db_migrates_bookmarks_to_annotations_and_drops_annotation_snapshots(tmp_path, monkeypatch):
    db_path = tmp_path / "squid.db"
    monkeypatch.setattr(stats_db, "_DB_PATH", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE bookmarks (
                msg_id INTEGER PRIMARY KEY,
                topic TEXT,
                agent TEXT,
                content TEXT,
                saved_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE message_annotations (
                msg_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                topic TEXT,
                agent TEXT,
                content TEXT,
                payload TEXT,
                created_at TEXT,
                updated_at TEXT,
                PRIMARY KEY (msg_id, kind)
            )"""
        )
        conn.execute(
            "INSERT INTO bookmarks (msg_id, topic, agent, content, saved_at) VALUES (7, 'squid', 'codex', 'old preview', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO message_annotations (msg_id, kind, topic, agent, content, payload, created_at, updated_at) VALUES (8, 'bad_response', 'squid', 'codex', 'bad preview', '{}', '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z')"
        )

    stats_db.init_db()

    bookmark = stats_db.get_message_annotation(7, "bookmark")
    bad = stats_db.get_message_annotation(8, "bad_response")
    assert bookmark is not None
    assert bookmark["created_at"] == "2026-01-01T00:00:00Z"
    assert bad is not None
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(message_annotations)")}
        bookmarks_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bookmarks'"
        ).fetchone()
    assert {"topic", "agent", "content"}.isdisjoint(columns)
    assert bookmarks_table is None


def test_stats_include_marked_bad_measure(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "fix it")
    marked_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(marked_id, "bad", None)
    other_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(other_id, "ok", None)
    stats_db.set_message_annotation(marked_id, "bad_response")

    turn_rows = stats_db.get_stats_by_turn(days=0)
    by_id = {row["msg_id"]: row for row in turn_rows}
    assert by_id[marked_id]["marked_bad"] == 1
    assert by_id[other_id]["marked_bad"] == 0

    aggregate = stats_db.get_aggregated_stats(
        period="daily",
        days=0,
        chart_series=[{"metric": "marked_bad", "agg": "sum"}],
    )[0]
    assert aggregate["marked_bad"] == 1
    assert aggregate["chart_marked_bad_sum"] == 1


def test_allocate_id_returns_incrementing_values_per_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    assert stats_db.allocate_id("flow_run") == "1"
    assert stats_db.allocate_id("flow_run") == "2"
    assert stats_db.allocate_id("other") == "1"


def test_init_db_backfills_source_for_existing_chat_messages(tmp_path, monkeypatch):
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
                   status_raw TEXT,
                   session_turn_index INTEGER,
                   lookback INTEGER DEFAULT 0,
                   quota_delta REAL,
                   quota_before REAL,
                   quota_after REAL,
                   completed_at TEXT,
                   created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
               )"""
        )
        conn.execute(
            "INSERT INTO chat_messages (topic, role, content, status) VALUES ('squid', 'user', 'old prompt', 'done')"
        )

    stats_db.init_db()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT source FROM chat_messages WHERE content='old prompt'").fetchone()

    assert row == ("human",)


def test_init_db_migrates_legacy_system_source_to_workflow(tmp_path, monkeypatch):
    db_path = tmp_path / "squid.db"
    monkeypatch.setattr(stats_db, "_DB_PATH", db_path)
    stats_db.init_db()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO chat_messages (topic, role, content, status, source) "
            "VALUES ('squid', 'user', 'handoff prompt', 'done', 'system')"
        )

    stats_db.init_db()

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT source FROM chat_messages WHERE content='handoff prompt'").fetchone()

    assert row == ("workflow",)


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

    assert [row[0] for row in rows] == [
        "clarev", "claude", "codex", "codrev", "currev",
        "cursor", "opencode", "operev", "pi", "pirev",
    ]


def test_init_db_seeds_review_agents_gated_per_harness_with_role_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr(stats_db, "SUPPORTED_HARNESSES", frozenset({"claudecode", "codex"}))
    monkeypatch.setattr(stats_db, "is_installed", lambda harness: harness in {"claudecode", "codex"})

    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        rows = dict(conn.execute("SELECT name, cwd FROM agents").fetchall())

    # Only harnesses reported installed get a review agent seeded.
    assert set(rows) == {"claude", "codex", "clarev", "codrev"}
    # claudecode gets the claude-specific CLAUDE.md shim; every other
    # harness reads the shared AGENTS.md directly (see context/roles/review/).
    assert rows["clarev"].endswith("/roles/review/claude")
    assert rows["codrev"].endswith("/roles/review")
    assert not rows["codrev"].endswith("/roles/review/claude")


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


def test_aggregated_stats_includes_count_chart_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.executemany(
            """INSERT INTO session_stats(
                   session_id, topic, agent, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, created_at
               ) VALUES (?, 'squid', 'codex', 0, 0, 0, 0, 0, ?)""",
            [
                ("s1", "2026-07-10T10:00:00Z"),
                ("s2", "2026-07-10T10:10:00Z"),
            ],
        )

    rows = stats_db.get_aggregated_stats(
        period="daily",
        days=0,
        chart_series=[
            {"metric": "turns", "agg": "sum"},
            {"metric": "sessions", "agg": "sum"},
        ],
    )

    assert rows[0]["total_turns"] == 2
    assert rows[0]["chart_turns_sum"] == 2
    assert rows[0]["chart_sessions_sum"] == 2


def test_aggregated_stats_returns_average_duration_and_duration_chart_aggs(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.executemany(
            """INSERT INTO session_stats(
                   session_id, topic, agent, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, duration_ms, created_at
               ) VALUES (?, 'squid', 'codex', 0, 0, 0, 0, 0, ?, ?)""",
            [
                ("s1", 1000, "2026-07-10T10:00:00Z"),
                ("s2", 3000, "2026-07-10T10:10:00Z"),
                ("s3", 8000, "2026-07-10T10:20:00Z"),
            ],
        )

    rows = stats_db.get_aggregated_stats(
        period="daily",
        days=0,
        chart_series=[
            {"metric": "duration", "agg": "avg"},
            {"metric": "duration", "agg": "p95"},
        ],
    )

    assert rows[0]["duration_ms"] == 4000
    assert rows[0]["chart_duration_avg"] == 4
    assert rows[0]["chart_duration_p95"] == 8


def test_aggregated_chart_average_uses_per_turn_stats_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.save_stats(
        "session-1",
        {"input_tokens": 3_160_000, "output_tokens": 0, "cost_usd": 0},
        topic="squid",
        agent="codex",
    )
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute(
            "UPDATE session_stats SET created_at=? WHERE session_id=?",
            ("2026-07-13T10:00:00Z", "session-1"),
        )

    for i, input_tokens in enumerate([100, 200, 300, 400, 500], start=1):
        user_id = stats_db.insert_user_message("squid", "codex", f"turn {i}")
        asst_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
        stats_db.update_assistant_message(asst_id, f"response {i}", "session-1", "done")
        stats_db.insert_run_event(asst_id, 0, "stats", json.dumps({"input_tokens": input_tokens, "output_tokens": i * 10}))
        with sqlite3.connect(tmp_path / "squid.db") as conn:
            conn.execute(
                "UPDATE chat_messages SET created_at=? WHERE id=?",
                ("2026-07-13T10:00:00Z", asst_id),
            )
            # Turn bucketing keys off the stats event's own created_at (when the
            # turn actually ended), not the chat_messages row's — stamp both.
            conn.execute(
                "UPDATE run_events SET created_at=? WHERE msg_id=? AND event_type='stats'",
                ("2026-07-13T10:00:00Z", asst_id),
            )

    rows = stats_db.get_aggregated_stats(
        period="daily",
        days=0,
        chart_series=[
            {"metric": "tokens_in", "agg": "sum"},
            {"metric": "tokens_in", "agg": "avg"},
            {"metric": "tokens_total", "agg": "sum"},
            {"metric": "tokens_total", "agg": "avg"},
        ],
    )

    assert rows[0]["input_tokens"] == 1500
    assert rows[0]["total_turns"] == 5
    assert rows[0]["chart_tokens_in_sum"] == 1500
    assert rows[0]["chart_tokens_in_avg"] == 300
    assert rows[0]["chart_tokens_total_sum"] == 1650
    assert rows[0]["chart_tokens_total_avg"] == 330


def test_aggregated_cache_hit_chart_uses_weighted_rate_not_sum(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.save_stats(
        "session-1",
        {"input_tokens": 100, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
        topic="squid",
        agent="codex",
    )
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute(
            "UPDATE session_stats SET created_at=? WHERE session_id=?",
            ("2026-07-13T10:00:00Z", "session-1"),
        )

    for i, payload in enumerate([
        {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 100, "cache_write_tokens": 0},
        {"input_tokens": 100, "output_tokens": 0, "cache_read_tokens": 50, "cache_write_tokens": 0},
    ], start=1):
        user_id = stats_db.insert_user_message("squid", "codex", f"turn {i}")
        asst_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
        stats_db.update_assistant_message(asst_id, f"response {i}", "session-1", "done")
        stats_db.insert_run_event(asst_id, 0, "stats", json.dumps(payload))
        with sqlite3.connect(tmp_path / "squid.db") as conn:
            conn.execute(
                "UPDATE chat_messages SET created_at=? WHERE id=?",
                ("2026-07-13T10:00:00Z", asst_id),
            )

    rows = stats_db.get_aggregated_stats(
        period="daily",
        days=0,
        chart_series=[{"metric": "cache_hit_rate", "agg": "sum"}],
    )

    assert rows[0]["chart_cache_hit_rate_sum"] == 75


def test_aggregated_stats_cutoff_ignores_created_at_string_format(tmp_path, monkeypatch):
    """session_stats.created_at is written via SQLite's CURRENT_TIMESTAMP
    ('YYYY-MM-DD HH:MM:SS'), not the 'YYYY-MM-DDTHH:MM:SSZ' format _stats_cutoff
    produces. Comparing them as raw strings sorts ' ' before 'T', so any row on
    the same calendar day as the cutoff was silently dropped regardless of its
    actual time of day (this is why selecting "1 day" showed only a few hours)
    — the days filter must compare via datetime(), not lexicographically."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr(stats_db, "_stats_cutoff", lambda days, *a, **kw: "2026-07-12T05:36:00Z")
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


def test_aggregated_stats_weekly_bucket_starts_on_sunday(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.executemany(
            """INSERT INTO session_stats(
                   session_id, topic, agent, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, created_at
               ) VALUES (?, 'squid', 'codex', ?, 0, 0, 0, 0, ?)""",
            [
                # Sunday 2026-07-12 through Saturday 2026-07-18 is one week.
                ("s1", 10, "2026-07-12T00:00:00Z"),
                ("s2", 20, "2026-07-18T23:00:00Z"),
                # Next week starts Sunday 2026-07-19.
                ("s3", 100, "2026-07-19T00:00:00Z"),
            ],
        )

    rows = stats_db.get_aggregated_stats(period="weekly", days=0)

    by_period = {row["period"]: row for row in rows}
    assert by_period["2026-07-12"]["input_tokens"] == 30
    assert by_period["2026-07-19"]["input_tokens"] == 100


def test_aggregated_stats_attributes_each_turn_to_its_own_day_bucket(tmp_path, monkeypatch):
    """Regression test: turn counts and token/cost sums used to be bucketed by two
    different timestamps — chat_messages.created_at (a turn's start) for counts,
    and session_stats.created_at for tokens. The latter was never updated past a
    session's first-ever save, so a session resumed days later would have every
    turn's tokens attributed to whichever day its *first* turn happened to land
    on, and any day with only an earlier turn (no matching session_stats period)
    was silently dropped entirely."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user1 = stats_db.insert_user_message("squid", "codex", "turn 1")
    asst1 = stats_db.insert_assistant_message("squid", "codex", user1, adhoc=False)
    stats_db.update_assistant_message(asst1, "response 1", "session-1", "done")
    stats_db.insert_run_event(asst1, 0, "stats", json.dumps({"input_tokens": 100}))
    stats_db.save_stats("session-1", {"input_tokens": 100, "output_tokens": 0, "cost_usd": 0.01},
                         topic="squid", agent="codex")

    user2 = stats_db.insert_user_message("squid", "codex", "turn 2")
    asst2 = stats_db.insert_assistant_message("squid", "codex", user2, adhoc=False)
    stats_db.update_assistant_message(asst2, "response 2", "session-1", "done")
    stats_db.insert_run_event(asst2, 0, "stats", json.dumps({"input_tokens": 900}))
    # Same session resumed 3 days later — session_stats is a single upserted row,
    # so its snapshot now carries turn 2's (larger, cumulative) numbers.
    stats_db.save_stats("session-1", {"input_tokens": 1000, "output_tokens": 0, "cost_usd": 0.10},
                         topic="squid", agent="codex")

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute("UPDATE chat_messages SET created_at=? WHERE id=?", ("2026-07-10T10:00:00Z", asst1))
        conn.execute("UPDATE run_events SET created_at=? WHERE msg_id=? AND event_type='stats'",
                     ("2026-07-10T10:00:00Z", asst1))
        conn.execute("UPDATE chat_messages SET created_at=? WHERE id=?", ("2026-07-13T10:00:00Z", asst2))
        conn.execute("UPDATE run_events SET created_at=? WHERE msg_id=? AND event_type='stats'",
                     ("2026-07-13T10:00:00Z", asst2))
        # save_stats now bumps created_at on every upsert, so this reflects the
        # session's latest activity (turn 2's day), not its first turn's.
        conn.execute("UPDATE session_stats SET created_at=? WHERE session_id=?",
                     ("2026-07-13T10:00:00Z", "session-1"))

    rows = stats_db.get_aggregated_stats(period="daily", days=0)
    by_period = {r["period"]: r for r in rows}

    # Turn 1's day still shows up with its own turn count — previously dropped
    # entirely, since only periods present on the session_stats side ever
    # appeared in the result.
    assert by_period["2026-07-10"]["total_turns"] == 1
    assert by_period["2026-07-10"]["input_tokens"] == 100
    # Turn 2's day carries turn 2's own stats event payload, not the latest
    # session_stats snapshot for the whole resumed session.
    assert by_period["2026-07-13"]["total_turns"] == 1
    assert by_period["2026-07-13"]["input_tokens"] == 900


def test_get_aggregated_stats_anchor_windows_relative_to_anchor_not_now(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.executemany(
            """INSERT INTO session_stats(
                   session_id, topic, agent, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, created_at
               ) VALUES (?, 'squid', 'codex', ?, 0, 0, 0, 0, ?)""",
            [
                ("in-window", 10, "2026-07-10T10:00:00Z"),
                # After the anchor — must be excluded even though it's "in the past" now.
                ("after-anchor", 999, "2026-07-14T10:00:00Z"),
                # More than 7d before the anchor — excluded by the lower bound.
                ("before-window", 999, "2026-07-01T10:00:00Z"),
            ],
        )

    rows = stats_db.get_aggregated_stats(period="daily", days=7, anchor="2026-07-12T00:00:00Z")

    total_in = sum(r.get("input_tokens") or 0 for r in rows)
    assert total_in == 10


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


def test_breakdown_cache_hit_chart_uses_weighted_rate_not_sum(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.executemany(
            """INSERT INTO session_stats(
                   session_id, topic, agent, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, created_at
               ) VALUES (?, 'squid', 'codex', ?, 0, ?, 0, 0, ?)""",
            [
                ("s1", 0, 100, "2026-07-10T10:00:00Z"),
                ("s2", 100, 50, "2026-07-10T10:10:00Z"),
            ],
        )

    rows = stats_db.get_stats_by_breakdown(
        period="daily",
        days=0,
        breakdown="agent",
        chart_series=[{"metric": "cache_hit_rate", "agg": "sum"}],
    )

    assert rows[0]["chart_cache_hit_rate_sum"] == 75



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
    with stats_db._connect() as conn:
        conn.execute(
            """UPDATE topics
               SET last_session_at='2026-06-12T12:00:00Z',
                   last_adhoc_at='2026-06-13T12:00:00Z',
                   last_at='2026-06-13T12:00:00Z'
               WHERE topic='squid' AND agent='codex'"""
        )
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
        "last_session_at": "2026-06-12T12:00:00Z",
        "last_adhoc_at": "2026-06-13T12:00:00Z",
        "last_model": None,
        "last_harness": None,
        "last_provider": None,
        "last_backend": None,
        "session_turns": 0,
        "adhoc_turns": 0,
        "agent_turns": 0,
        "live_turns": 0,
    }]


def test_topics_management_summary_backfills_lane_times_from_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.upsert_topic("squid", "codex", last_prompt="session prompt", adhoc=False)
    stats_db.upsert_topic("squid", "codex", last_prompt="adhoc prompt", adhoc=True)
    session_user_id = stats_db.insert_user_message("squid", "codex", "session prompt")
    adhoc_user_id = stats_db.insert_user_message("squid", "codex", "adhoc prompt")
    session_msg_id = stats_db.insert_assistant_message("squid", "codex", session_user_id, adhoc=False)
    adhoc_msg_id = stats_db.insert_assistant_message("squid", "codex", adhoc_user_id, adhoc=True)
    with stats_db._connect() as conn:
        conn.execute(
            """UPDATE topics
               SET last_session_at=NULL,
                   last_adhoc_at=NULL,
                   last_at='2026-06-13T12:00:00Z'
               WHERE topic='squid' AND agent='codex'"""
        )
        conn.execute(
            "UPDATE chat_messages SET created_at='2026-06-12T12:00:00Z' WHERE id=?",
            (session_msg_id,),
        )
        conn.execute(
            "UPDATE chat_messages SET created_at='2026-06-13T12:00:00Z' WHERE id=?",
            (adhoc_msg_id,),
        )

    lane = stats_db.get_topics_management_summary(include_hidden=True)[0]["agents"][0]

    assert lane["last_session_at"] == "2026-06-12T12:00:00Z"
    assert lane["last_adhoc_at"] == "2026-06-13T12:00:00Z"


def test_delete_topic_preserves_session_stats_for_consumption_history(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.upsert_topic("squid", "codex", last_prompt="keep stats", adhoc=False)
    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    asst_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(asst_id, "response", "session-1", "done")
    stats_db.insert_run_event(asst_id, 0, "stats", json.dumps({"input_tokens": 100, "output_tokens": 20}))
    stats_db.save_stats(
        "session-1",
        {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.25},
        topic="squid",
        agent="codex",
        backend="codex",
    )
    stats_db.set_topic_session("squid", "codex", "session-1", "/repo", None)

    assert stats_db.delete_topic("squid") is True

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE topic='squid'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM topic_sessions WHERE topic='squid'").fetchone()[0] == 0
        stat = conn.execute("SELECT topic, agent, input_tokens FROM session_stats WHERE session_id='session-1'").fetchone()

    assert dict(stat) == {"topic": "squid", "agent": "codex", "input_tokens": 100}
    assert stats_db.get_stats_filter_options() == {"agents": ["codex"], "topics": ["squid"]}
    rows = stats_db.get_aggregated_stats(period="daily", days=0, topic="squid")
    assert rows[0]["input_tokens"] == 100
    assert rows[0]["output_tokens"] == 20
    assert stats_db.get_stats_by_turn(days=0, topic="squid") == []


def test_delete_topic_agent_preserves_stats_for_all_modes(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.upsert_topic("squid", "codex", last_prompt="session prompt", adhoc=False)
    stats_db.upsert_topic("squid", "codex", last_prompt="adhoc prompt", adhoc=True)
    for session_id, adhoc, tokens in [
        ("session-1", False, 100),
        ("adhoc-1", True, 40),
    ]:
        user_id = stats_db.insert_user_message("squid", "codex", f"{session_id} prompt")
        asst_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=adhoc)
        stats_db.update_assistant_message(asst_id, "response", session_id, "done")
        stats_db.save_stats(
            session_id,
            {"input_tokens": tokens, "output_tokens": 1, "adhoc": adhoc},
            topic="squid",
            agent="codex",
            backend="codex",
        )

    stats_db.delete_topic_agent("squid", "codex", adhoc=True)
    stats_db.delete_topic_agent("squid", "codex", adhoc=False)

    rows = stats_db.get_stats_by_agent(days=0, topic="squid", agent="codex")
    assert rows[0]["input_tokens"] == 140
    assert rows[0]["sessions"] == 2
    assert stats_db.get_stats_filter_options() == {"agents": ["codex"], "topics": ["squid"]}


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


def test_recent_prompts_use_flow_route_prefix_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message(
        "squid",
        "codex",
        "review this",
        flow_route="#squid@codex>@review",
    )
    stats_db.insert_assistant_message(
        "squid",
        "codex",
        user_id,
        flow_run_id="flow1",
        flow_route="#squid@codex>@review",
    )

    assert stats_db.get_recent_prompts(limit=5) == ["#squid@codex>@review review this"]


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


def test_history_items_prefer_stored_completed_at(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "long turn")
    asst_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(asst_id, "response", "session-1", "done")
    stats_db.insert_run_event(asst_id, 0, "stats", json.dumps({"input_tokens": 10}))
    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute(
            "UPDATE chat_messages SET created_at=?, completed_at=? WHERE id=?",
            ("2026-07-15T10:50:07Z", "2026-07-15T11:07:00Z", asst_id),
        )
        conn.execute(
            "UPDATE run_events SET created_at=? WHERE msg_id=? AND event_type='stats'",
            ("2026-07-15T11:06:43Z", asst_id),
        )

    item = stats_db.get_messages_flat(topic="squid", agent="codex", limit=1)["items"][0]
    status = stats_db.get_message(asst_id)

    assert item["timestamp"] == "2026-07-15T10:50:07Z"
    assert item["completed_at"] == "2026-07-15T11:07:00Z"
    assert status["completed_at"] == "2026-07-15T11:07:00Z"


def test_get_messages_flat_uses_per_turn_stats_not_latest_session_row(tmp_path, monkeypatch):
    """Regression test: history view previously showed the session's latest stats
    (session_stats, overwritten on every resumed turn) on every historical message
    instead of each message's own turn stats. Per-turn numbers must come from each
    message's own run_events "stats" snapshot instead."""
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user1 = stats_db.insert_user_message("squid", "codex", "first")
    asst1 = stats_db.insert_assistant_message("squid", "codex", user1, adhoc=False)
    stats_db.update_assistant_message(asst1, "first response", "session-1", "done")
    stats_db.insert_run_event(asst1, 0, "stats", json.dumps(
        {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.1, "duration_ms": 5000}
    ))
    stats_db.save_stats(
        "session-1",
        {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.1, "duration_ms": 5000},
        topic="squid", agent="codex",
    )

    user2 = stats_db.insert_user_message("squid", "codex", "second")
    asst2 = stats_db.insert_assistant_message("squid", "codex", user2, adhoc=False)
    stats_db.update_assistant_message(asst2, "second response", "session-1", "done")
    stats_db.insert_run_event(asst2, 0, "stats", json.dumps(
        {"input_tokens": 200, "output_tokens": 20, "cost_usd": 0.2, "duration_ms": 8000}
    ))
    # Same session resumed for turn 2 -- session_stats gets overwritten with turn 2's numbers,
    # so it no longer reflects turn 1 at all.
    stats_db.save_stats(
        "session-1",
        {"input_tokens": 200, "output_tokens": 20, "cost_usd": 0.2, "duration_ms": 8000},
        topic="squid", agent="codex",
    )

    items = stats_db.get_messages_flat(topic="squid", agent="codex", limit=10)["items"]
    by_id = {i["id"]: i for i in items}

    assert by_id[asst1]["stats"]["duration_ms"] == 5000
    assert by_id[asst1]["stats"]["cost_usd"] == 0.1
    assert by_id[asst2]["stats"]["duration_ms"] == 8000
    assert by_id[asst2]["stats"]["cost_usd"] == 0.2

    # get_message (single-message lookup) must agree per-message too.
    assert stats_db.get_message(asst1)["stats"]["duration_ms"] == 5000
    assert stats_db.get_message(asst2)["stats"]["duration_ms"] == 8000


def test_history_orders_by_completed_at_not_message_id(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    slow_user_id = stats_db.insert_user_message("squid", "codex", "slow")
    slow_asst_id = stats_db.insert_assistant_message("squid", "codex", slow_user_id, adhoc=False)
    stats_db.update_assistant_message(slow_asst_id, "slow response", "session-slow", "done")
    stats_db.insert_run_event(slow_asst_id, 0, "stats", json.dumps({"input_tokens": 10}), created_at="2026-07-15T12:15:25Z")

    fast_user_id = stats_db.insert_user_message("squid", "deepseek", "fast")
    fast_asst_id = stats_db.insert_assistant_message("squid", "deepseek", fast_user_id, adhoc=False)
    stats_db.update_assistant_message(fast_asst_id, "fast response", "session-fast", "done")
    stats_db.insert_run_event(fast_asst_id, 0, "stats", json.dumps({"input_tokens": 10}), created_at="2026-07-15T12:10:28Z")

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute(
            "UPDATE chat_messages SET created_at=?, completed_at=? WHERE id=?",
            ("2026-07-15T12:06:46Z", "2026-07-15T12:15:25Z", slow_asst_id),
        )
        conn.execute(
            "UPDATE chat_messages SET created_at=?, completed_at=? WHERE id=?",
            ("2026-07-15T12:09:14Z", "2026-07-15T12:10:28Z", fast_asst_id),
        )

    history = stats_db.get_messages_flat(topic="squid", limit=10)["items"]

    assert [item["id"] for item in history[:2]] == [slow_asst_id, fast_asst_id]
    assert slow_asst_id < fast_asst_id


def test_history_around_uses_completed_at_keyset(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    def done_turn(prompt, response, completed_at):
        user_id = stats_db.insert_user_message("squid", "codex", prompt)
        asst_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
        stats_db.update_assistant_message(asst_id, response, "session-1", "done")
        stats_db.insert_run_event(asst_id, 0, "stats", json.dumps({"input_tokens": 1}), created_at=completed_at)
        with sqlite3.connect(tmp_path / "squid.db") as conn:
            conn.execute("UPDATE chat_messages SET completed_at=? WHERE id=?", (completed_at, asst_id))
        return asst_id

    oldest = done_turn("oldest", "oldest response", "2026-07-15T10:00:00Z")
    target = done_turn("target", "target response", "2026-07-15T10:20:00Z")
    newest = done_turn("newest", "newest response", "2026-07-15T10:30:00Z")
    older_near = done_turn("older near", "older near response", "2026-07-15T10:10:00Z")

    window = stats_db.get_messages_around(target, before=1, after=1, topic="squid", agent="codex")

    assert window["found"] is True
    assert [item["id"] for item in window["items"]] == [newest, target, older_near]
    assert window["has_older"] is True
    assert window["has_newer"] is False
    assert window["older_cursor"]["id"] == older_near
    assert window["newer_cursor"]["id"] == newest

    older_page = stats_db.get_messages_from_cursor(
        "older",
        window["older_cursor"]["completed_at"],
        window["older_cursor"]["id"],
        limit=1,
        topic="squid",
        agent="codex",
    )

    assert [item["id"] for item in older_page["items"]] == [oldest]
    assert older_page["has_more"] is False


def test_history_around_flow_resolves_numeric_flow_id(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    ordinary_user = stats_db.insert_user_message("squid", "codex", "ordinary")
    ordinary_asst = stats_db.insert_assistant_message("squid", "codex", ordinary_user, adhoc=False)
    stats_db.update_assistant_message(ordinary_asst, "ordinary response", "session-1", "done")
    stats_db.insert_run_event(ordinary_asst, 0, "stats", json.dumps({"input_tokens": 1}), created_at="2026-07-15T10:00:00Z")

    flow_user = stats_db.insert_user_message(
        "squid", "codex", "flow origin", flow_run_id="71", flow_route="#squid@codex>@revu",
    )
    flow_asst = stats_db.insert_assistant_message(
        "squid", "codex", flow_user, adhoc=False, flow_run_id="71", flow_route="#squid@codex>@revu",
    )
    stats_db.update_assistant_message(flow_asst, "flow response", "session-flow", "done")
    stats_db.insert_run_event(flow_asst, 0, "stats", json.dumps({"input_tokens": 1}), created_at="2026-07-15T10:05:00Z")

    window = stats_db.get_messages_around_flow("71", before=0, after=0)

    assert window["found"] is True
    assert window["target_id"] == flow_asst
    assert window["flow_run_id"] == "71"
    assert window["target_id"] != 71
    assert window["items"][0]["id"] == flow_asst
    assert window["items"][0]["flow_run_id"] == "71"


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
    assert content_row["completed_at"] is not None
    empty_row = stats_db.get_message(empty_asst_id)
    assert empty_row["status"] == "error"
    assert empty_row["content"] == ""
    assert empty_row["completed_at"] is not None


def test_history_prefers_stored_completion_time_over_legacy_stats_time(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    first_user = stats_db.insert_user_message("squid", "codex", "first")
    first_asst = stats_db.insert_assistant_message("squid", "codex", first_user)
    stats_db.update_assistant_message(first_asst, "first response", "session-1", "done")
    stats_db.insert_run_event(first_asst, 0, "stats", "{}", created_at="2026-07-15T12:30:00Z")

    second_user = stats_db.insert_user_message("squid", "codex", "second")
    second_asst = stats_db.insert_assistant_message("squid", "codex", second_user)
    stats_db.update_assistant_message(second_asst, "second response", "session-2", "done")

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute("UPDATE chat_messages SET completed_at=? WHERE id=?", ("2026-07-15T12:10:00Z", first_asst))
        conn.execute("UPDATE chat_messages SET completed_at=? WHERE id=?", ("2026-07-15T12:20:00Z", second_asst))

    history = stats_db.get_messages_flat(topic="squid", limit=10)["items"]
    assert [item["id"] for item in history[:2]] == [second_asst, first_asst]


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


def test_quota_delta_preserves_negative_meter_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    stats_db.save_stats(
        "session-1",
        {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25},
        topic="squid",
        agent="codex",
    )
    stats_db.save_quota_delta("session-1", 42.5, 40.0)

    user_id = stats_db.insert_user_message("squid", "codex", "hello")
    asst_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(asst_id, "response", "session-1", "done")
    stats_db.insert_run_event(asst_id, 0, "stats", json.dumps(
        {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25}
    ))
    stats_db.update_message_quota_snapshot(asst_id, 42.5, 40.0)

    assert stats_db.get_stats_by_topic(days=0)[0]["quota_delta"] == -2.5
    by_msg = {row["msg_id"]: row for row in stats_db.get_stats_by_turn(days=0)}
    assert by_msg[asst_id]["quota_delta"] == -2.5


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


def test_stats_status_filter_separates_shell_from_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "normal")
    complete_id = stats_db.insert_assistant_message("squid", "codex", user_id)
    stats_db.update_assistant_message(complete_id, "ok", "session-normal", "done")

    shell_user_id = stats_db.insert_user_message("squid", "codex", "!pwd")
    shell_id = stats_db.insert_assistant_message("squid", "codex", shell_user_id, source="shell")
    stats_db.update_assistant_message(shell_id, "/tmp", None, "done")

    assert [r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, status="done")] == [complete_id]
    assert [r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, status="shell")] == [shell_id]
    assert {
        r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, status="done,shell")
    } == {complete_id, shell_id}
    assert stats_db.get_aggregated_stats(period="daily", days=0, status="done")[0]["total_turns"] == 1
    assert stats_db.get_aggregated_stats(period="daily", days=0, status="shell")[0]["total_turns"] == 1


def test_stats_filters_by_single_or_multi_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    single_user = stats_db.insert_user_message("squid", "codex", "single")
    single_asst = stats_db.insert_assistant_message("squid", "codex", single_user, adhoc=False)
    stats_db.update_assistant_message(single_asst, "ok", "session-single", "done")
    stats_db.insert_run_event(single_asst, 0, "stats", json.dumps({"input_tokens": 10}))

    multi_user = stats_db.insert_user_message("squid", "codex", "multi", flow_run_id="flow-1", flow_route="#squid@codex>#squid@revu")
    multi_asst = stats_db.insert_assistant_message(
        "squid", "codex", multi_user, adhoc=False, flow_run_id="flow-1", flow_route="#squid@codex>#squid@revu",
    )
    stats_db.update_assistant_message(multi_asst, "ok", "session-multi", "done")
    stats_db.insert_run_event(multi_asst, 0, "stats", json.dumps({"input_tokens": 20}))

    assert [r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, flow="single")] == [single_asst]
    assert [r["msg_id"] for r in stats_db.get_stats_by_turn(days=0, flow="multi")] == [multi_asst]
    assert stats_db.get_aggregated_stats(period="daily", days=0, flow="single")[0]["input_tokens"] == 10
    assert stats_db.get_aggregated_stats(period="daily", days=0, flow="multi")[0]["input_tokens"] == 20


def test_terminal_non_success_turns_are_stats_rows_without_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user1 = stats_db.insert_user_message("squid", "codex", "stop this")
    cancelled_asst = stats_db.insert_assistant_message("squid", "codex", user1, adhoc=False)
    assert stats_db.mark_assistant_cancelled(cancelled_asst, "Cancelled")

    user2 = stats_db.insert_user_message("squid", "claude", "fail this")
    error_asst = stats_db.insert_assistant_message("squid", "claude", user2, adhoc=True)
    stats_db.update_assistant_message(error_asst, "boom", None, "error")

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        conn.execute(
            "UPDATE chat_messages SET completed_at='2026-07-15T10:00:00Z' WHERE id=?",
            (cancelled_asst,),
        )
        conn.execute(
            "UPDATE chat_messages SET completed_at='2026-07-15T10:05:00Z' WHERE id=?",
            (error_asst,),
        )

    by_turn = {row["msg_id"]: row for row in stats_db.get_stats_by_turn(days=0)}
    assert by_turn[cancelled_asst]["status"] == "cancelled"
    assert by_turn[cancelled_asst]["cancelled_turns"] == 1
    assert by_turn[cancelled_asst]["error_turns"] == 0
    assert by_turn[cancelled_asst]["input_tokens"] == 0
    assert by_turn[error_asst]["status"] == "error"
    assert by_turn[error_asst]["error_turns"] == 1

    aggregate = stats_db.get_aggregated_stats(period="daily", days=0)[0]
    assert aggregate["total_turns"] == 2
    assert aggregate["cancelled_turns"] == 1
    assert aggregate["error_turns"] == 1
    assert aggregate["done_turns"] == 0


def test_stats_filters_include_terminal_rows_without_session_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user = stats_db.insert_user_message("squid", "codex", "stop this")
    asst = stats_db.insert_assistant_message("squid", "codex", user, adhoc=False)
    stats_db.mark_assistant_cancelled(asst, "Cancelled")

    assert stats_db.get_stats_filter_options() == {"agents": ["codex"], "topics": ["squid"]}


def test_cancelled_resumed_turn_keeps_session_id_before_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user = stats_db.insert_user_message("squid", "codex", "stop this")
    asst = stats_db.insert_assistant_message("squid", "codex", user, adhoc=False)

    assert stats_db.attach_assistant_session(asst, "session-1")
    assert stats_db.mark_assistant_cancelled(asst, "Cancelled")

    with sqlite3.connect(tmp_path / "squid.db") as conn:
        row = conn.execute(
            "SELECT session_id, session_turn_index, status FROM chat_messages WHERE id=?",
            (asst,),
        ).fetchone()

    assert row == ("session-1", None, "cancelled")


def test_cancelled_turn_recovers_available_run_event_attributes(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user = stats_db.insert_user_message(
        "squid",
        "codex",
        "stop this",
        context_ids=[7, 8],
        mem=True,
        mem_revision="rev-1",
    )
    asst = stats_db.insert_assistant_message("squid", "codex", user, adhoc=False)
    stats_db.insert_run_event(asst, 0, "status", "Thinking\n")
    stats_db.insert_run_event(asst, 1, "text", "partial answer")
    stats_db.insert_run_event(asst, 2, "tool", json.dumps({"name": "Read", "path": "app.py"}))
    stats_db.insert_run_event(asst, 3, "stats", json.dumps({"session_id": "session-1"}))

    assert stats_db.mark_assistant_cancelled(asst, "Cancelled")

    row = stats_db.get_message(asst)
    assert row["status"] == "cancelled"
    assert row["content"] == "partial answer"
    assert row["session_id"] == "session-1"
    assert row["status_raw"] == "Thinking\n"
    assert json.loads(row["context"]) == [{"name": "Read", "path": "app.py"}]
    assert json.loads(row["prompt_context"]) == {
        "pins": [7, 8],
        "mem": True,
        "mem_revision": "rev-1",
    }


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


def test_omitted_path_is_ineligible_for_revert(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    repo = "/tmp/project"
    user_id = stats_db.insert_user_message("squid", "codex", "prompt")
    msg_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    context = json.dumps([{
        "name": "GitDiff",
        "repo": repo,
        "files": [
            {"status": "M", "path": "small.txt"},
            {"status": "M", "path": "huge.txt"},
        ],
        "diff": "diff --git a/small.txt b/small.txt\n",
        "omitted_paths": ["huge.txt"],
    }])
    stats_db.update_assistant_message(msg_id, "resp", "session-1", "done", context=context)

    assert stats_db.get_diff_revert_eligibility(msg_id, repo) == {
        "small.txt": "revertable",
        "huge.txt": "diff_too_large",
    }


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
        {"role": "user", "content": "implement change", "topic": "squid", "agent": "codex"},
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
        ), "topic": "squid", "agent": "codex"},
    ]


def test_get_message_previews_fetches_lightweight_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "prompt preview")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.update_assistant_message(assistant_id, "assistant response preview", "session-1", "done")

    pending_user_id = stats_db.insert_user_message("squid", "codex", "pending prompt preview")
    pending_id = stats_db.insert_assistant_message("squid", "codex", pending_user_id, adhoc=False)

    previews = stats_db.get_message_previews([assistant_id, pending_id], max_chars=10)
    assert previews == [
        {"id": assistant_id, "preview": "assistant "},
        {"id": pending_id, "preview": "pending pr"},
    ]
    assert "full diff should not be injected" not in previews[1]["preview"]
    assert "sqd-squid-1234-deadbe" not in previews[1]["preview"]


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
    stats_db.add_bookmark(bookmarked_id)

    newer_user_id = stats_db.insert_user_message("squid", "codex", "newer prompt")
    newer_id = stats_db.insert_assistant_message("squid", "codex", newer_user_id, adhoc=False)
    stats_db.update_assistant_message(newer_id, "needle unbookmarked response", "session-2", "done")

    search = stats_db.search_messages("needle", topic="squid", agent="codex", bookmarked=True, limit=1)

    assert [item["id"] for item in search["items"]] == [bookmarked_id]


def test_history_can_filter_bookmarks_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    bookmarked_user_id = stats_db.insert_user_message("squid", "codex", "bookmarked prompt")
    bookmarked_id = stats_db.insert_assistant_message("squid", "codex", bookmarked_user_id, adhoc=False)
    stats_db.update_assistant_message(bookmarked_id, "bookmarked response", "session-1", "done")
    stats_db.add_bookmark(bookmarked_id)

    newer_user_id = stats_db.insert_user_message("squid", "codex", "newer prompt")
    newer_id = stats_db.insert_assistant_message("squid", "codex", newer_user_id, adhoc=False)
    stats_db.update_assistant_message(newer_id, "unbookmarked response", "session-2", "done")

    history = stats_db.get_messages_flat(topic="squid", agent="codex", bookmarked=True, limit=1)

    assert history["total"] == 1
    assert [item["id"] for item in history["items"]] == [bookmarked_id]


def test_history_can_filter_marked_bad_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    marked_user_id = stats_db.insert_user_message("squid", "codex", "marked prompt")
    marked_id = stats_db.insert_assistant_message("squid", "codex", marked_user_id, adhoc=False)
    stats_db.update_assistant_message(marked_id, "marked bad response", "session-1", "done")
    stats_db.set_message_annotation(marked_id, "bad_response")

    newer_user_id = stats_db.insert_user_message("squid", "codex", "newer prompt")
    newer_id = stats_db.insert_assistant_message("squid", "codex", newer_user_id, adhoc=False)
    stats_db.update_assistant_message(newer_id, "ordinary response", "session-2", "done")

    history = stats_db.get_messages_flat(topic="squid", agent="codex", marked_bad=True, limit=1)

    assert history["total"] == 1
    assert [item["id"] for item in history["items"]] == [marked_id]
    assert history["items"][0]["marked_bad"] == 1


def test_search_messages_can_filter_marked_bad_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    marked_user_id = stats_db.insert_user_message("squid", "codex", "marked prompt")
    marked_id = stats_db.insert_assistant_message("squid", "codex", marked_user_id, adhoc=False)
    stats_db.update_assistant_message(marked_id, "needle marked bad response", "session-1", "done")
    stats_db.set_message_annotation(marked_id, "bad_response")

    newer_user_id = stats_db.insert_user_message("squid", "codex", "newer prompt")
    newer_id = stats_db.insert_assistant_message("squid", "codex", newer_user_id, adhoc=False)
    stats_db.update_assistant_message(newer_id, "needle ordinary response", "session-2", "done")

    search = stats_db.search_messages("needle", topic="squid", agent="codex", marked_bad=True, limit=1)

    assert [item["id"] for item in search["items"]] == [marked_id]
    assert search["items"][0]["marked_bad"] == 1


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


def test_flow_route_filters_history_and_search(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    flow_route = "#squid@codex>@revu"
    flow_run_id = "flow-test"
    origin_user = stats_db.insert_user_message(
        "squid", "codex", "needle origin prompt",
        flow_run_id=flow_run_id, flow_route=flow_route,
    )
    origin_asst = stats_db.insert_assistant_message(
        "squid", "codex", origin_user, adhoc=False,
        flow_run_id=flow_run_id, flow_route=flow_route,
    )
    stats_db.update_assistant_message(origin_asst, "needle origin response", "origin-session", "done")

    target_user = stats_db.insert_user_message(
        "squid", "revu", "needle target prompt",
        flow_run_id=flow_run_id, flow_route=flow_route,
    )
    target_asst = stats_db.insert_assistant_message(
        "squid", "revu", target_user, adhoc=False,
        flow_run_id=flow_run_id, flow_route=flow_route,
    )
    stats_db.update_assistant_message(target_asst, "needle target response", "target-session", "done")

    other_user = stats_db.insert_user_message("squid", "revu", "needle unrelated prompt")
    other_asst = stats_db.insert_assistant_message("squid", "revu", other_user, adhoc=False)
    stats_db.update_assistant_message(other_asst, "needle unrelated response", "other-session", "done")

    history = stats_db.get_messages_flat(flow_route=flow_route, limit=10)
    assert {item["id"] for item in history["items"]} == {origin_asst, target_asst}
    assert all(item["flow_run_id"] == flow_run_id for item in history["items"])

    content_search = stats_db.search_messages("needle", flow_route=flow_route, limit=10)
    assert {item["id"] for item in content_search["items"]} == {origin_asst, target_asst}

    prompt_search = stats_db.search_prompts("needle", flow_route=flow_route, limit=10)
    assert {item["id"] for item in prompt_search["items"]} == {origin_asst, target_asst}


def test_flow_route_filter_matches_legacy_handoff_prompt_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    route = "#squid@codex!>@revuqwen!"
    origin_user = stats_db.insert_user_message("squid", "codex", "needle origin prompt")
    origin_asst = stats_db.insert_assistant_message("squid", "codex", origin_user, adhoc=True)
    stats_db.update_assistant_message(origin_asst, "needle origin response", "origin-session", "done")

    handoff = "\n".join([
        "Squid route chain handoff.",
        f"Route: {route}",
        "Previous step: @codex",
        "Current step: @revuqwen!",
        "Original prompt: needle origin prompt",
    ])
    target_user = stats_db.insert_user_message("squid", "revuqwen", handoff, source="workflow")
    target_asst = stats_db.insert_assistant_message("squid", "revuqwen", target_user, adhoc=True)
    stats_db.update_assistant_message(target_asst, "needle target response", "target-session", "done")

    other_user = stats_db.insert_user_message("squid", "revuqwen", "needle unrelated prompt")
    other_asst = stats_db.insert_assistant_message("squid", "revuqwen", other_user, adhoc=True)
    stats_db.update_assistant_message(other_asst, "needle unrelated response", "other-session", "done")

    history = stats_db.get_messages_flat(flow_route="#squid@codex>@revuqwen", limit=10)
    assert {item["id"] for item in history["items"]} == {origin_asst, target_asst}
    origin_item = next(item for item in history["items"] if item["id"] == origin_asst)
    assert origin_item["flow_route"] == route

    content_search = stats_db.search_messages("needle", flow_route="#squid@codex>@revuqwen", limit=10)
    assert {item["id"] for item in content_search["items"]} == {origin_asst, target_asst}

    prompt_search = stats_db.search_prompts("needle", flow_route="#squid@codex>@revuqwen", limit=10)
    assert {item["id"] for item in prompt_search["items"]} == {origin_asst, target_asst}


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


def test_status_raw_exact_final_response_is_dropped_on_done(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)

    stats_db.update_assistant_message(
        assistant_id, "final response", "session-1", "done",
        status_raw=" final response\n",
    )

    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] is None


def test_status_raw_trailing_final_response_is_stripped_on_done(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)

    stats_db.update_assistant_message(
        assistant_id, "final response", "session-1", "done",
        status_raw="Working...\nfinal response",
    )

    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] == "Working..."


def test_status_raw_repeated_final_response_is_dropped_on_done(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)

    stats_db.update_assistant_message(
        assistant_id, "final response", "session-1", "done",
        status_raw="final response\nfinal response",
    )

    row = stats_db.get_message(assistant_id)
    assert row["status_raw"] is None


def test_completed_run_status_raw_strips_trailing_final_response(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    stats_db.init_db()

    user_id = stats_db.insert_user_message("squid", "codex", "test")
    assistant_id = stats_db.insert_assistant_message("squid", "codex", user_id, adhoc=False)
    stats_db.insert_run_event(assistant_id, 0, "status", "Working...\n")
    stats_db.insert_run_event(assistant_id, 1, "status", "final response")
    stats_db.insert_run_event(assistant_id, 2, "text", "final response")
    stats_db.insert_run_event(assistant_id, 3, "done", None)

    assert stats_db.get_completed_run_status_raw(assistant_id) == "Working..."


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
    assert before["status"] == "pending"

    stats_db.mark_worktree_synced("t", "901", "/repo")

    after = stats_db.get_worktrees("t", "901")[0]
    assert after["status"] == "synced"
    assert after["last_used_at"] >= before["last_used_at"]
