import json

from agent import context_sync


def _sync_with_context(tmp_path, monkeypatch):
    context_dir = tmp_path / "context"
    squid_home = tmp_path / "home"
    context_dir.mkdir()

    monkeypatch.setattr(context_sync, "CONTEXT_DIR", str(context_dir))
    monkeypatch.setattr(context_sync, "SQUID_HOME", str(squid_home))
    monkeypatch.setattr(context_sync, "_rsync", lambda: True)

    context_sync.sync_now()
    return context_dir


def test_sync_now_does_not_create_mcp_context_files(tmp_path, monkeypatch):
    context_dir = _sync_with_context(tmp_path, monkeypatch)

    assert not (context_dir / ".mcp.json").exists()
    assert not (context_dir / ".codex" / "config.toml").exists()
    assert not (context_dir / ".cursor" / "mcp.json").exists()
    assert not (context_dir / "opencode.json").exists()


def test_sync_now_removes_legacy_squid_mcp_context_files(tmp_path, monkeypatch):
    context_dir = tmp_path / "context"
    (context_dir / ".codex").mkdir(parents=True)
    (context_dir / ".cursor").mkdir()
    context_dir.mkdir(exist_ok=True)
    (context_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"squid": {"command": "python"}}}),
        encoding="utf-8",
    )
    (context_dir / ".codex" / "config.toml").write_text(
        "# BEGIN SQUID MANAGED MCP\n[mcp_servers.squid]\ncommand = \"python\"\n# END SQUID MANAGED MCP\n",
        encoding="utf-8",
    )
    (context_dir / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"squid": {"command": "python"}}}),
        encoding="utf-8",
    )
    (context_dir / "opencode.json").write_text(
        json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {"squid": {"command": ["python"]}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(context_sync, "CONTEXT_DIR", str(context_dir))
    monkeypatch.setattr(context_sync, "SQUID_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(context_sync, "_rsync", lambda: True)

    context_sync.sync_now()

    assert not (context_dir / ".mcp.json").exists()
    assert not (context_dir / ".codex").exists()
    assert not (context_dir / ".cursor").exists()
    assert not (context_dir / "opencode.json").exists()


def test_sync_now_preserves_non_squid_mcp_entries(tmp_path, monkeypatch):
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (context_dir / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "squid": {"command": "python"},
                "other": {"command": "other-tool"},
            },
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(context_sync, "CONTEXT_DIR", str(context_dir))
    monkeypatch.setattr(context_sync, "SQUID_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(context_sync, "_rsync", lambda: True)

    context_sync.sync_now()

    config = json.loads((context_dir / ".mcp.json").read_text(encoding="utf-8"))
    assert config == {"mcpServers": {"other": {"command": "other-tool"}}}
