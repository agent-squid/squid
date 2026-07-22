import hashlib

import pytest

from agent import memory
from agent.memory import (
    code_roots_prompt_block,
    read_topic_memory,
    topic_memory_prompt_block,
    topic_memory_squid_config,
    write_topic_memory,
    write_topic_memory_squid_code_roots,
)
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
    assert data["revision"] == hashlib.sha256(b"").hexdigest()
    assert not (tmp_path / "topics").exists()


def test_write_topic_memory_creates_lowercase_path(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    data = write_topic_memory("Squid", "Use explicit context.")

    assert data["topic"] == "squid"
    assert data["exists"] is True
    assert data["revision"] == hashlib.sha256(b"Use explicit context.").hexdigest()
    assert (tmp_path / "topics" / "squid" / "memory.md").read_text() == "Use explicit context."


def test_topic_memory_revision_changes_only_when_content_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    first = write_topic_memory("squid", "Use explicit context.")
    same = write_topic_memory("squid", "Use explicit context.")
    changed = write_topic_memory("squid", "Use updated context.")

    assert same["revision"] == first["revision"]
    assert changed["revision"] != first["revision"]


def test_topic_memory_prompt_block_skips_empty_and_wraps_content(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    assert topic_memory_prompt_block("squid") is None

    write_topic_memory("squid", "\nRemember the design decision.\n")
    block = topic_memory_prompt_block("squid")

    assert '<topic_memory topic="squid">' in block
    assert "Remember the design decision." in block
    assert block.endswith("</topic_memory>")


def test_topic_memory_squid_config_reads_code_roots_and_skip_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    write_topic_memory("squid", """---
squid:
  code_roots:
    - /work/squid
  code_roots_skipped: true
---

Notes.
""")

    config = topic_memory_squid_config("squid")

    assert config["code_roots"] == ["/work/squid"]
    assert config["code_roots_skipped"] is False
    assert config["code_roots_missing"] is False


def test_isolated_code_roots_prompt_mentions_squid_publish_command():
    prompt = memory.code_roots_prompt_block(["/repo"], isolated=True, topic="squid")

    assert "Run `squid-publish --topic squid`." in prompt
    assert "MCP" not in prompt
    assert "mcp__squid__publish" not in prompt
    assert "ToolSearch" not in prompt
    assert "Do not run git init, commit, push, branch, tag" in prompt


def test_isolated_code_roots_prompt_forbids_git_recovery():
    block = code_roots_prompt_block(["/work/squid"], isolated=True)

    assert "intentionally not a Git repository" in block
    assert "Do not search for, switch to, or suggest working in another checkout or source repository" in block
    assert "If asked to commit, push, publish, branch, tag, release, or open a PR" in block
    assert "squid-publish --topic <topic> --tag <tag>" in block
    assert "squid-publish --topic <topic>" in block
    assert "Do not provide manual Git commands as a workaround" in block
    assert "working in the real repo" not in block
    assert "git tag -f" not in block
    assert "run that against the process's working directory" not in block


def test_frontmatter_closing_delimiter_must_be_standalone(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    write_topic_memory("squid", """---
squid:
  code_roots:
    - /work/squid
  note: --- not a delimiter
  other: "--- also not a delimiter"
---

Notes.
""")

    config = topic_memory_squid_config("squid")

    assert config["code_roots"] == ["/work/squid"]


def test_write_topic_memory_squid_code_roots_preserves_markdown_body(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    write_topic_memory("squid", "## Notes\n\nKeep this.")
    data = write_topic_memory_squid_code_roots("squid", code_roots_skipped=True)

    assert data["squid"]["code_roots_skipped"] is True
    assert data["content"].startswith(
        "---\nsquid:\n  # code_roots:\n  #   - /absolute/path/to/repo\n  code_roots_skipped: true\n---\n"
    )
    assert "## Notes\n\nKeep this." in data["content"]


def test_write_topic_memory_squid_code_roots_replaces_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TOPICS_CONTEXT_DIR", tmp_path / "topics")

    write_topic_memory_squid_code_roots("squid", code_roots_skipped=True)
    data = write_topic_memory_squid_code_roots("squid", code_roots=["/work/squid"])

    assert data["squid"]["code_roots"] == ["/work/squid"]
    assert data["squid"]["code_roots_skipped"] is False
    assert "code_roots_skipped" not in data["content"]
