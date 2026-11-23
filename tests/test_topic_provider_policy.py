import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent import memory, server, topic_provider_policy as policy
from agent.topic_queue import QueueItem, TopicWorker


@pytest.fixture
def providers(monkeypatch):
    memory.write_topic_memory("provider_test", """---
squid:
  allow_providers: [openai_pers, anthropic_pers]
  deny_providers: [openai, anthropic]
---
""")

    def resolve(agent):
        if agent["provider"] == "unknown":
            raise ValueError("Provider is missing")
        provider = SimpleNamespace(id=agent["provider"])
        runtime = SimpleNamespace(provider=provider, fingerprint=provider.id)
        return "echo", provider.id, "echo", runtime

    def get_agent(name):
        return {"name": name, "provider": name}

    monkeypatch.setattr(server, "_resolve_agent_runtime", resolve)
    monkeypatch.setattr(server, "get_agent", get_agent)
    monkeypatch.setattr(server, "get_topic", lambda topic: {"agent": "openai"})
    monkeypatch.setattr(server, "get_default_agent", lambda: get_agent("openai_pers"))
    monkeypatch.setattr(server, "topic_memory_squid_config", lambda topic: {})
    monkeypatch.setattr(server, "maybe_sync", AsyncMock())
    monkeypatch.setattr(server, "_run_realtime_chat", AsyncMock())
    monkeypatch.setattr(policy, "_challenges", {})


def test_memory_policy_is_dynamic_and_inherited_by_dotted_subtopics():
    assert policy.rule_for("squid") is None
    memory.write_topic_memory("squid", """---
squid:
  allow_providers: [openai_pers, anthropic]
  deny_providers: [anthropic]
---
""")
    rule = policy.rule_for("squid.special")
    assert rule.decision("openai_pers") is None
    assert rule.decision("anthropic") == "provider_denied"
    assert rule.decision(None) == "provider_unverified"
    assert rule.decision("other") == "provider_approval_required"
    assert policy.rule_for("elsewhere") is None

    memory.write_topic_memory_squid_code_roots("squid", code_roots=["/project"])
    assert policy.rule_for("squid.special") == rule

    memory.write_topic_memory("squid", "---\nsquid:\n  deny_providers: [openai]\n---\n")
    assert policy.rule_for("squid").decision("openai") == "provider_denied"
    assert policy.rule_for("squid").decision("other") is None
    memory.write_topic_memory("squid", "Prose without provider preferences.")
    assert policy.rule_for("squid.special") is None


@pytest.mark.parametrize("content", [
    "---\nsquid:\n  allow_providers: null\n---\n",
    "---\nsquid:\n  deny_providers: openai\n---\n",
    "---\nsquid:\n  deny_providers: [openai\n---\n",
    "---\nsquid:\n  deny_providers: [openai]\n",
])
def test_invalid_provider_rules_fail_closed(content):
    memory.write_topic_memory("squid", content)
    with pytest.raises(policy.InvalidPolicy):
        policy.rule_for("squid")


@pytest.mark.asyncio
async def test_invalid_memory_edit_is_rejected_and_existing_rule_is_preserved(providers):
    original = policy.rule_for("provider_test")
    response = await server.put_topic_memory_route(
        "provider_test", server.TopicMemoryRequest(content="---\nsquid:\n  deny_providers: openai\n---\n"),
    )
    assert response.status_code == 400
    assert policy.rule_for("provider_test") == original


@pytest.mark.asyncio
async def test_implicit_selection_uses_eligible_default_and_explicit_denial_surfaces(providers):
    req = {"topic": "provider_test", "message": "hello", "adhoc": True}
    denied = await server._realtime_chat_start({**req, "agent": "openai"}, "browser")
    assert denied["error"] == "provider_denied"
    assert denied["provider"] == "openai"
    assert "approval_token" not in denied
    unverified = await server._realtime_chat_start({**req, "agent": "unknown"}, "browser")
    assert unverified["error"] == "provider_unverified"
    result = await server._prepare_chat_turn(**req, agent=None)
    assert result["agent"] == result["provider"] == "openai_pers"


@pytest.mark.asyncio
async def test_approval_is_bound_to_browser_request_and_runtime_and_can_only_be_used_once(providers, monkeypatch):
    req = {"topic": "provider_test", "message": "hello", "agent": "other", "adhoc": True}
    initial = await server._realtime_chat_start(req, "browser")
    assert initial["error"] == "provider_approval_required"
    approved = {**req, "provider_approval": initial["approval_token"]}
    assert (await server._realtime_chat_start(approved, "another-browser"))["error"] == initial["error"]
    assert (await server._realtime_chat_start({**approved, "message": "changed"}, "browser"))["error"] == initial["error"]
    assert "approval_token" not in await server._realtime_chat_start({**approved, "source": "workflow"}, "browser")

    monkeypatch.setattr(server.codex_subscription, "agent_revision", lambda agent: 1)
    changed = await server._realtime_chat_start(approved, "browser")
    assert changed["error"] == initial["error"]
    approved["provider_approval"] = changed["approval_token"]
    assert (await server._realtime_chat_start(approved, "browser"))["ok"]
    assert (await server._realtime_chat_start(approved, "browser"))["error"] == initial["error"]
    assert server._run_realtime_chat.call_count == 1


@pytest.mark.asyncio
async def test_automation_cannot_request_one_time_approval(providers):
    for source in ("watch", "continuation", "workflow"):
        response = await server._prepare_chat_turn(
            topic="provider_test", message="hello", agent="other", source=source,
        )
        body = json.loads(response.body)
        assert body["error"] == "provider_approval_required"
        assert "approval_token" not in body


@pytest.mark.asyncio
async def test_worker_blocks_denied_provider_before_creating_runner(providers, monkeypatch):
    from agent import resolve, runners

    runtime = SimpleNamespace(provider=SimpleNamespace(id="openai"), fingerprint="runtime")
    monkeypatch.setattr(resolve, "resolve_agent", lambda *args: runtime)
    runner = Mock()
    monkeypatch.setattr(runners, "runner_for_agent", runner)
    item = QueueItem(1, "provider_test", "agent", "hello", [], harness="codex", provider="openai")
    item.approved_runtime = policy.Runtime(item.topic, item.agent, "runtime", None, 0)
    memory.write_topic_memory(item.topic, "---\nsquid:\n  deny_providers: [openai]\n---\n")
    await TopicWorker(item.topic)._process(item)
    assert "denied provider openai" in (await item.out_q.get())["_error"]
    assert await item.out_q.get() is None
    runner.assert_not_called()
