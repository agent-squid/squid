import pytest

from agent import memory
from agent.memory import read_topic_memory, topic_memory_prompt_block, write_topic_memory
from agent.topics import normalize_topic_slug


def test_normalize_topic_slug_lowercases():
    assert normalize_topic_slug("Squid") == "squid"
    assert normalize_topic_slug("My_Topic1") == "my_topic1"


def test_normalize_topic_slug_rejects_invalid_chars():
    with pytest.raises(ValueError):
        normalize_topic_slug("bad-topic")


def test_missing_topic_memory_does_not_create_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    data = read_topic_memory("Squid")

    assert data["topic"] == "squid"
    assert data["exists"] is False
    assert data["content"] == ""
    assert not (tmp_path / "topics").exists()


def test_write_topic_memory_creates_lowercase_path(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    data = write_topic_memory("Squid", "Use explicit context.")

    assert data["topic"] == "squid"
    assert data["exists"] is True
    assert (tmp_path / "topics" / "squid" / "memory.md").read_text() == "Use explicit context."


def test_topic_memory_prompt_block_skips_empty_and_wraps_content(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    assert topic_memory_prompt_block("squid") is None

    write_topic_memory("squid", "\nRemember the design decision.\n")
    block = topic_memory_prompt_block("squid")

    assert '<topic_memory topic="squid">' in block
    assert "Remember the design decision." in block
    assert block.endswith("</topic_memory>")
