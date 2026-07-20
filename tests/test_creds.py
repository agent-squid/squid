import json

from agent import creds


def test_max_budget_roundtrip_per_gauge(tmp_path, monkeypatch):
    monkeypatch.setattr(creds, "_CREDS_PATH", tmp_path / "squid-creds.json")

    creds.save_max_budget("kimi", 25.0)
    creds.save_max_budget("deepseek", 10.0)

    assert creds.get_max_budget("kimi") == 25.0
    assert creds.get_max_budget("deepseek") == 10.0

    creds.clear_max_budget("kimi")
    assert creds.get_max_budget("kimi") is None
    assert creds.get_max_budget("deepseek") == 10.0


def test_deepseek_max_budget_legacy_key_migrates(tmp_path, monkeypatch):
    path = tmp_path / "squid-creds.json"
    path.write_text(json.dumps({"deepseek_max_budget": 20.0}))
    monkeypatch.setattr(creds, "_CREDS_PATH", path)

    # pre-registry installs stored the cap under a flat deepseek-only key
    assert creds.get_max_budget("deepseek") == 20.0

    creds.save_max_budget("deepseek", 30.0)
    data = json.loads(path.read_text())
    assert "deepseek_max_budget" not in data
    assert creds.get_max_budget("deepseek") == 30.0
