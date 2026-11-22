import asyncio
import base64
import json
import os
import sys
import threading

import pytest
import yaml
from fastapi.testclient import TestClient

from agent import auth_sessions, codex_subscription as subscription
from agent import providers, runners, server, stats_db
from agent.topic_queue import TopicDispatcher


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(subscription, "ROOT", tmp_path / "provider-homes")
    monkeypatch.setattr(subscription, "_agent_revisions", {})
    monkeypatch.setattr(stats_db, "_DB_PATH", tmp_path / "squid.db")
    monkeypatch.setattr(providers, "PROVIDERS", {
        "openai": providers.Provider("openai"),
        "cope": providers._validate_provider("cope", {
            "supported_apis": ["/v1/responses"],
            "auth": {"type": "subscription", "dedicated_login": True},
        }),
    })
    stats_db.init_db()
    return tmp_path


def write_auth(home, subject="personal", token="test-access"):
    claims = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode().rstrip("=")
    path = home / "auth.json"
    path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
        "id_token": f"header.{claims}.signature",
        "access_token": token,
        "account_id": "workspace",
    }}))
    path.chmod(0o600)


def test_provider_rejects_credential_redirect_and_home_alias(isolated, monkeypatch):
    with pytest.raises(ValueError, match="native Codex"):
        providers._validate_provider("cope", {
            "supported_apis": ["/v1/responses"], "base_url": "https://example.invalid",
            "auth": {"type": "subscription", "dedicated_login": True},
        })
    home = subscription.ensure_home("cope")
    monkeypatch.setenv("CODEX_HOME", str(home))
    with pytest.raises(ValueError, match="aliases the host"):
        subscription.ensure_home("cope")


def test_codex_probe_rejects_a_credential_file_replaced_after_home_validation(isolated):
    home = subscription.ensure_home("cope")
    write_auth(home)
    assert subscription.account_identity(home)

    replacement = isolated / "auth.json"
    write_auth(isolated)
    replacement.chmod(0o644)
    replacement.replace(home / "auth.json")

    with pytest.raises(ValueError, match="credential file must be private"):
        subscription.account_identity(home)


def test_provider_launch_uses_only_its_login_and_keeps_home(isolated, monkeypatch):
    home = subscription.ensure_home("cope")
    monkeypatch.setenv("OPENAI_API_KEY", "host-synthetic")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "host-synthetic")
    monkeypatch.setenv("CODEX_HOME", str(isolated / "host"))

    async def collect(account):
        return [line async for line in runners._stream_lines(
            [sys.executable, "-c", "import os, json; print(json.dumps({k: os.environ.get(k) "
             'for k in ("HOME", "CODEX_HOME", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN")}))'],
            codex_home=home, codex_account=account, backend="codex",
        )]

    with pytest.raises(runners.CLIAuthRequired):
        asyncio.run(collect(None))
    write_auth(home)
    account = subscription.account_identity(home)
    write_auth(home, token="refreshed-synthetic-token")
    assert subscription.account_identity(home) == account
    result, = asyncio.run(collect(account))
    assert json.loads(result) == {
        "HOME": os.environ["HOME"], "CODEX_HOME": str(home),
        "OPENAI_API_KEY": None, "CODEX_ACCESS_TOKEN": None,
    }
    write_auth(home, subject="another-user")
    with pytest.raises(runners.CLIError, match="account changed"):
        asyncio.run(collect(account))


def test_provider_sign_in_does_not_share_environment_and_serializes(isolated, monkeypatch):
    replacement = isolated / "replacement"
    replacement.mkdir()
    write_auth(replacement, subject="new-personal")
    executable = isolated / "codex"
    executable.write_text(f"#!{sys.executable}\n"
                          "import os, pathlib\n"
                          f"replacement = pathlib.Path({str(replacement)!r}) / 'auth.json'\n"
                          "replacement.replace(pathlib.Path(os.environ['CODEX_HOME'], 'auth.json'))\n")
    executable.chmod(0o700)
    monkeypatch.setattr(auth_sessions, "CODEX_PATH", str(executable))
    home = subscription.ensure_home("cope")
    write_auth(home)
    before = subscription.account_identity(home)
    stats_db.upsert_agent("personal", "codex", "cope", None, None)
    stats_db.set_topic_session("test", "personal", "old-account", str(isolated))

    async def exercise():
        with subscription.lock(home, login=False):
            with pytest.raises(auth_sessions.AuthSessionError, match="busy"):
                await auth_sessions.create_session("codex", 100, 10, provider_id="cope")
        session = await auth_sessions.create_session("codex", 100, 10, provider_id="cope")
        await asyncio.wait_for(session._closed.wait(), timeout=5)
        assert session.returncode == 0
        assert subscription.account_identity(home) != before
        assert stats_db.get_topic_session("test", "personal") is None
        write_auth(replacement)
        session = await auth_sessions.create_session("codex", 100, 10, provider_id="cope")
        await asyncio.wait_for(session._closed.wait(), timeout=5)
        assert session.returncode == 0
        assert subscription.account_identity(home) != before
        after = subscription.account_identity(home)
        write_auth(home, token="refreshed")
        assert subscription.account_identity(home) == after

    asyncio.run(exercise())


def test_queued_turn_refuses_stale_binding(isolated):
    provider = providers.require_provider("cope")
    stats_db.upsert_agent("personal", "codex", "cope", None, None)
    binding = subscription.bind(provider, "personal")
    subscription.invalidate_agent("personal")
    providers.PROVIDERS.pop("cope")
    user = stats_db.insert_user_message("test", "personal", "test")
    assistant = stats_db.insert_assistant_message("test", "personal", user)
    stats_db.attach_assistant_session(assistant, "old-account")

    async def exercise():
        dispatcher = TopicDispatcher()
        queue, _, worker = await dispatcher.dispatch(
            topic="test", prompt="test", context_history=[], harness="codex", provider="cope",
            agent="personal", codex_binding=binding, msg_id=assistant,
        )
        try:
            results = []
            while (item := await asyncio.wait_for(queue.get(), timeout=3)) is not None:
                results.append(item)
            assert any("provider changed" in item.get("_error", "") for item in results if isinstance(item, dict))
        finally:
            await worker.q.put(None)

    asyncio.run(exercise())
    message = stats_db.get_message(assistant)
    assert message["status"] == "error"
    assert message["session_id"] is None


def test_recorded_store_survives_provider_removal_and_rejects_quota(isolated):
    home = subscription.ensure_home("cope")
    sessions = home / "sessions"
    sessions.mkdir()
    log = sessions / "turn-example-id.jsonl"
    log.write_text("{}\n")
    stats_db.save_stats("example-id", {}, agent="personal", harness="codex",
                        provider="cope", codex_home=str(home), dedicated_codex=True)
    user_msg = stats_db.insert_user_message("test", "personal", "hi")
    msg_id = stats_db.insert_assistant_message("test", "personal", user_msg)
    stats_db.record_codex_provider_home(msg_id, str(home))
    providers.PROVIDERS.pop("cope")
    client = TestClient(server.app)
    response = client.get("/session-log", params={"agent": "personal", "session_id": "example-id"})
    assert response.json()["path"] == str(log)
    snapshots = {"before": 1, "after": 2}
    assert client.post("/stats/quota-delta", json={**snapshots, "session_id": "example-id"}).status_code == 409
    assert client.post(f"/chat/{msg_id}/quota-delta", json=snapshots).status_code == 409


def test_yaml_rejects_incompatible_and_stale_config_without_changing_store(isolated, monkeypatch):
    from agent import config

    path = isolated / "squid.yaml"
    content = """server: {host: 127.0.0.1, port: 7010}
agent: {first_byte_timeout: 10, response_timeout: 20}
providers:
  openai:
    auth: {type: subscription}
    supported_apis: [/v1/responses]
  cope:
    auth: {type: subscription}
    supported_apis: [/v1/responses]
"""
    path.write_text(content)
    settings = yaml.safe_load(content)
    monkeypatch.setattr(config, "_USER_CONFIG", path)
    monkeypatch.setattr(server, "_USER_CONFIG", path)
    monkeypatch.setattr(server, "_cfg", settings)
    monkeypatch.setattr(providers, "_cfg", settings)
    monkeypatch.setattr(server, "_LOCALFILE_ROOTS", [])
    providers.reload_providers()
    stats_db.upsert_agent("personal", "codex", "cope", None, None)
    stats_db.set_agent_home_mode("personal", "blank_home")
    client = TestClient(server.app)
    updated = content.replace("  cope:\n    auth: {type: subscription}",
                              "  cope:\n    auth: {type: subscription, dedicated_login: true}")
    url = "/config/yaml"
    revision = config.config_revision()
    response = client.put(url, json={"content": updated, "revision": revision})
    assert response.status_code == 400
    assert "personal" in response.json()["error"]
    assert path.read_text() == content
    assert not providers.require_provider("cope").dedicated_login

    stats_db.set_agent_home_mode("personal", "user_home")
    stats_db.set_topic_session("test", "personal", "host-history", str(isolated))
    response = client.put(url, json={"content": updated, "revision": "stale"})
    assert response.status_code == 409
    assert path.read_text() == content
    assert stats_db.get_topic_session("test", "personal")["session_id"] == "host-history"

    response = client.put(url, json={"content": updated, "revision": revision})
    assert response.status_code == 200, response.text
    assert path.read_text() == updated
    assert providers.require_provider("cope").dedicated_login
    assert stats_db.get_topic_session("test", "personal") is None
    assert subscription.agent_revision("personal") == 1


def test_codex_turn_resumes_from_provider_store(isolated, monkeypatch):
    executable = isolated / "codex"
    executable.write_text(f"#!{sys.executable}\n" + """
import json
import os
import pathlib
import sys

home = pathlib.Path(os.environ["CODEX_HOME"])
assert (home / "auth.json").is_file()
assert "OPENAI_API_KEY" not in os.environ
assert [arg for arg in sys.argv if arg.startswith("openai_base_url=")] == [
    'openai_base_url="https://chatgpt.com/backend-api/codex"',
]
session = "local-session-fixture"
resume = "resume" in sys.argv
directory = home / "sessions"
log = directory / f"turn-{session}.jsonl"
if resume:
    assert session in sys.argv
    assert log.is_file()
else:
    directory.mkdir(exist_ok=True)
    log.write_text("{}")
print(json.dumps({"type": "thread.started", "thread_id": session}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "resumed" if resume else "new"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""")
    executable.chmod(0o700)
    monkeypatch.setattr(runners, "CODEX_PATH", str(executable))
    stats_db.upsert_agent("personal", "codex", "cope", None, str(isolated))
    home = subscription.ensure_home("cope")
    write_auth(home)

    async def turn():
        user_id = stats_db.insert_user_message("test", "personal", "test")
        assistant_id = stats_db.insert_assistant_message("test", "personal", user_id)
        dispatcher = TopicDispatcher()
        queue, _, worker = await dispatcher.dispatch(
            topic="test", prompt="test", context_history=[], harness="codex", provider="cope",
            agent="personal", cwd=str(isolated), configured_cwd=str(isolated), msg_id=assistant_id,
        )
        response = []
        try:
            while (item := await asyncio.wait_for(queue.get(), timeout=5)) is not None:
                if isinstance(item, dict):
                    assert "_error" not in item, item
                else:
                    response.append(item)
            return "".join(response)
        finally:
            await worker.q.put(None)

    assert asyncio.run(turn()) == "new"
    assert asyncio.run(turn()) == "resumed"
    assert stats_db.get_stats("local-session-fixture")["codex_home"] == str(home)


def test_watch_grant_keeps_account_on_refresh_and_requires_reapproval_on_replacement(isolated, monkeypatch):
    from types import SimpleNamespace

    from agent import watch_runs

    stats_db.upsert_agent("personal", "codex", "cope", None, str(isolated))
    monkeypatch.setattr("agent.memory.topic_memory_squid_config", lambda topic: {})
    home = subscription.ensure_home("cope")
    write_auth(home)
    grant = {
        "agent": "personal", "agent_revision": watch_runs.agent_revision("personal", "test"),
        "flow_route": None, "publication_json": None, "revoked_at": None,
        "expires_at": 100, "max_runs": 1, "max_attempts": 1,
    }
    watch = {"topic": "test", "status": "active", "expires_at": 100}
    policy = SimpleNamespace(execution_enabled=True, max_runs=1, max_attempts=1)
    service = SimpleNamespace(
        _poll_guard=lambda *args: True,
        configuration=SimpleNamespace(effective_policy=lambda topic: policy),
    )

    write_auth(home, token="refreshed")
    assert watch_runs.authorized(service, None, watch, grant, now=1)
    write_auth(home, subject="replacement")
    assert not watch_runs.authorized(service, None, watch, grant, now=1)

    (home / "auth.json").unlink()
    assert not watch_runs.authorized(service, None, watch, grant, now=1)
    with pytest.raises(ValueError, match="Sign in to subscription provider"):
        watch_runs.agent_revision("personal", "test")


def claude_provider(monkeypatch):
    provider = providers._validate_provider("clape", {
        "supported_apis": ["/v1/messages"],
        "auth": {"type": "subscription", "dedicated_login": True},
    })
    monkeypatch.setitem(providers.PROVIDERS, provider.id, provider)
    return provider


def test_claude_provider_resolves_to_one_shot_and_rejects_ambiguous_api(isolated, monkeypatch):
    from agent.resolve import resolve_agent

    claude_provider(monkeypatch)
    assert resolve_agent("claudecode", "clape", protocol="interactive-cli").protocol == "oneshot-cli"
    with pytest.raises(ValueError, match="requires the claudecode harness"):
        resolve_agent("codex", "clape")
    with pytest.raises(ValueError, match="unambiguous API"):
        providers._validate_provider("unclear", {
            "supported_apis": ["/v1/messages", "/v1/responses"],
            "auth": {"type": "subscription", "dedicated_login": True},
        })


def test_claude_native_status_binds_private_credential_not_cached_identity(isolated, monkeypatch):
    from agent import config
    from types import SimpleNamespace

    provider = claude_provider(monkeypatch)
    home = subscription.ensure_home(provider.id, "claudecode")
    monkeypatch.setattr(config, "CLAUDE_PATH", "/mock/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-fixture")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "host-fixture")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/host")
    status = {
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
        "forcedLoginMethod": "claudeai", "configDirectory": str(home.resolve()),
        "email": "one@example.invalid", "orgId": "same-org",
    }

    probes = 0

    def run(argv, *, cwd, env, **kwargs):
        nonlocal probes
        probes += 1
        assert cwd == home and argv[-3:] == ["auth", "status", "--json"]
        assert argv[1:3] == ["--setting-sources", ""]
        assert env["CLAUDE_CONFIG_DIR"] == str(home.resolve())
        assert env["ANTHROPIC_CONFIG_DIR"] == str(home.resolve() / ".no-console-profile")
        assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in env
        return SimpleNamespace(stdout=json.dumps(status), returncode=0)

    monkeypatch.setattr(subscription.subprocess, "run", run)
    monkeypatch.setattr(subscription, "sys", SimpleNamespace(platform="linux"))
    credentials = home / ".credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fixture-one"}}))
    credentials.chmod(0o600)
    first = subscription.bind(provider, "personal", "claudecode").account
    assert subscription.account_verified(first)
    assert subscription.bind(provider, "personal", "claudecode", verify_claude=False).account == first
    assert probes == 1
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fixture-two"}}))
    assert subscription.bind(provider, "personal", "claudecode", verify_claude=False).account != first
    assert probes == 1
    monkeypatch.setattr(subscription, "sys", SimpleNamespace(platform="darwin"))
    assert subscription.account_identity(home, "claudecode") == subscription.CLAUDE_UNVERIFIED
    assert not subscription.account_verified(subscription.CLAUDE_UNVERIFIED)
    del status["email"]
    with pytest.raises(ValueError, match="account and organization"):
        subscription.account_identity(home, "claudecode")
    status["authMethod"] = "api_key"
    with pytest.raises(ValueError, match="native claude.ai"):
        subscription.account_identity(home, "claudecode")


def test_claude_turn_invalidates_changed_account_at_process_launch(isolated, monkeypatch):
    home = subscription.ensure_home("clape", "claudecode")
    monkeypatch.setattr(subscription, "_claude_account", lambda home, **kwargs: "other-account")

    async def collect():
        return [line async for line in runners._stream_lines(
            [sys.executable, "-c", "raise AssertionError('must not start')"],
            backend="claudecode", codex_home=home, codex_account="original-account",
        )]

    with pytest.raises(runners.CLIError, match="account changed"):
        asyncio.run(collect())


def test_claude_checks_provider_revision_after_auth_probe(isolated, monkeypatch):
    home = subscription.ensure_home("clape", "claudecode")

    def account_identity(*args):
        subscription.invalidate_agent("personal")
        return "original-account"

    async def spawn(*args, **kwargs):
        pytest.fail("An obsolete provider binding reached subprocess launch")

    monkeypatch.setattr(subscription, "account_identity", account_identity)
    monkeypatch.setattr(runners, "CLAUDE_PATH", "/unused/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def collect():
        async for _ in runners.run_claude(
            "test", agent="personal", codex_home=home, codex_account="original-account",
            subscription_revision=0,
        ):
            pass

    with pytest.raises(runners.CLIError, match="provider changed before launch"):
        asyncio.run(collect())
    with subscription.lock(home, login=True):
        pass


def test_cancelled_claude_probe_keeps_store_locked_until_probe_exits(isolated, monkeypatch):
    home = subscription.ensure_home("clape", "claudecode")
    release = threading.Event()

    async def exercise():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def account_identity(*args):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            return "original-account"

        monkeypatch.setattr(subscription, "account_identity", account_identity)

        async def launch():
            async for _ in runners._stream_lines(
                [sys.executable, "-c", "raise AssertionError('must not start')"],
                backend="claudecode", codex_home=home, codex_account="original-account",
            ):
                pytest.fail("Cancelled turn started")

        turn = asyncio.create_task(launch())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            turn.cancel()
            await asyncio.sleep(0)
            with pytest.raises(subscription.SubscriptionBusy):
                subscription.lock(home, login=True)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await turn
        with subscription.lock(home, login=True):
            pass

    asyncio.run(exercise())


def test_sign_in_rechecks_provider_after_auth_probe(isolated, monkeypatch):
    home = subscription.ensure_home("cope")

    def account_identity(*args, **kwargs):
        providers.PROVIDERS.pop("cope")
        return None

    def spawn(*args, **kwargs):
        pytest.fail("A removed provider reached sign-in")

    monkeypatch.setattr(subscription, "account_identity", account_identity)
    monkeypatch.setattr(auth_sessions, "CODEX_PATH", "/unused/codex")
    monkeypatch.setattr(auth_sessions, "_spawn_pty", spawn)

    with pytest.raises(auth_sessions.AuthSessionError, match="provider changed before sign-in"):
        asyncio.run(auth_sessions.create_session("codex", 80, 24, provider_id="cope"))
    with subscription.lock(home, login=True):
        pass


def test_dedicated_cli_error_reaps_process_and_releases_login_lock(isolated, monkeypatch):
    home = subscription.ensure_home("cope")
    write_auth(home)
    executable = isolated / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import time\n"
        "print('{\"type\": \"error\", \"message\": \"fixture failure\"}', flush=True)\n"
        "time.sleep(10)\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(runners, "CODEX_PATH", str(executable))

    async def collect():
        async for _ in runners.run_codex(
            "test", codex_home=home, codex_account=subscription.account_identity(home),
        ):
            pass

    async def exercise():
        with pytest.raises(runners.CLIError, match="fixture failure"):
            await asyncio.wait_for(collect(), timeout=5)
        with subscription.lock(home, login=True):
            pass

    asyncio.run(exercise())


def test_claude_partial_log_uses_recorded_provider_after_agent_removal(isolated):
    home = subscription.ensure_home("clape", "claudecode")
    projects = home / "projects" / "project"
    projects.mkdir(parents=True)
    log = projects / "claude-fixture.jsonl"
    log.write_text("{}\n")
    user = stats_db.insert_user_message("test", "personal", "test")
    assistant = stats_db.insert_assistant_message("test", "personal", user)
    stats_db.record_codex_provider_home(assistant, str(home), "account", "claudecode")
    stats_db.attach_assistant_session(assistant, "claude-fixture")
    client = TestClient(server.app)
    response = client.get("/session-log", params={"agent": "personal", "session_id": "claude-fixture"})
    assert response.json()["path"] == str(log)
    assert client.post(f"/chat/{assistant}/quota-delta", json={"before": 1, "after": 2}).status_code == 409


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_claude_provider_sign_in_and_resume_share_store(isolated, monkeypatch, platform):
    from types import SimpleNamespace

    from agent import config

    monkeypatch.setattr(subscription, "sys", SimpleNamespace(platform=platform))

    provider = claude_provider(monkeypatch)
    home = subscription.ensure_home(provider.id, "claudecode")
    executable = isolated / "claude"
    executable.write_text(f"#!{sys.executable}\n" + """
import json
import os
import pathlib
import sys

home = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
assert "ANTHROPIC_API_KEY" not in os.environ
assert sys.argv[sys.argv.index("--setting-sources") + 1] == ""
profile = home / "fixture-login"
if "login" in sys.argv:
    assert "--claudeai" in sys.argv
    if (home / "hold-login").exists():
        import time
        time.sleep(60)
    profile.touch()
    credentials = home / ".credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fixture"}}))
    credentials.chmod(0o600)
elif "status" in sys.argv:
    with (home / "status-count").open("a") as probes:
        probes.write("probe" + chr(10))
    print(json.dumps({
        "configDirectory": str(home), "apiProvider": "firstParty", "forcedLoginMethod": "claudeai",
        "loggedIn": profile.exists(), "authMethod": "claude.ai" if profile.exists() else "none",
        "email": profile.read_text() or "personal@example.invalid" if profile.exists() else None,
        "orgId": "fixture-org",
    }))
    sys.exit(0 if profile.exists() else 1)
else:
    assert "--print" in sys.argv and profile.is_file()
    assert os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    session = "claude-turn-fixture"
    resume = "--resume" in sys.argv
    if resume:
        assert sys.argv[sys.argv.index("--resume") + 1] == session
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}))
    print(json.dumps({"type": "result", "result": "resumed" if resume else "new", "usage": {}, "session_id": session}))
""")
    executable.chmod(0o700)
    for owner in (config, auth_sessions, runners):
        monkeypatch.setattr(owner, "CLAUDE_PATH", str(executable))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-fixture")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")
    stats_db.upsert_agent("personal", "claudecode", "clape", None, str(isolated))

    async def exercise():
        dispatcher = TopicDispatcher()

        async def turn():
            user = stats_db.insert_user_message("test", "personal", "test")
            assistant = stats_db.insert_assistant_message("test", "personal", user)
            queue, _, worker = await dispatcher.dispatch(
                topic="test", prompt="test", context_history=[], harness="claudecode", provider="clape",
                agent="personal", cwd=str(isolated), configured_cwd=str(isolated), msg_id=assistant,
            )
            output = []
            while (event := await asyncio.wait_for(queue.get(), timeout=5)) is not None:
                output.append(event)
            return output, worker

        results, worker = await turn()
        try:
            assert any("cli-auth-required:claudecode:clape" in event.get("_error", "")
                       for event in results if isinstance(event, dict))
            session = await auth_sessions.create_session("claudecode", 100, 10, provider_id="clape")
            await asyncio.wait_for(session._closed.wait(), timeout=5)
            assert session.returncode == 0
            probe_log = home / "status-count"
            probes_after_login = probe_log.read_text()
            first_binding = subscription.bind(provider, "personal", "claudecode", verify_claude=False)
            assert first_binding.can_reuse
            assert probe_log.read_text() == probes_after_login

            results, _ = await turn()
            assert "new" in results
            results, _ = await turn()
            assert "resumed" in results
            if platform == "darwin":
                hold_login = home / "hold-login"
                hold_login.touch()
                try:
                    cancelled = await auth_sessions.create_session("claudecode", 100, 10, provider_id="clape")
                    assert await auth_sessions.cancel_session(cancelled.id)
                finally:
                    hold_login.unlink()
                binding = subscription.bind(provider, "personal", "claudecode", verify_claude=False)
                assert binding.account == first_binding.account
                results, _ = await turn()
                assert "resumed" in results

                (home / "fixture-login").write_text("replacement@example.invalid")
                with pytest.raises(ValueError, match="Claude account changed outside Squid"):
                    subscription.account_identity(home, "claudecode")
                results, _ = await turn()
                assert any("Claude account changed outside Squid" in event.get("_error", "")
                           for event in results if isinstance(event, dict))
                probes_before_replacement = probe_log.read_text()
                replacement = await auth_sessions.create_session("claudecode", 100, 10, provider_id="clape")
                await asyncio.wait_for(replacement._closed.wait(), timeout=5)
                assert replacement.returncode == 0
                assert probe_log.read_text() == probes_before_replacement + "probe\n"
                binding = subscription.bind(provider, "personal", "claudecode", verify_claude=False)
                assert binding.can_reuse and binding.account != first_binding.account
                results, _ = await turn()
                assert "new" in results
        finally:
            await worker.q.put(None)

    asyncio.run(exercise())
    assert stats_db.get_stats("claude-turn-fixture")["codex_home"] == str(home)


def test_claude_unverified_metadata_leaves_login_available(isolated, monkeypatch):
    claude_provider(monkeypatch)
    subscription.ensure_home("clape", "claudecode")

    def unidentified(home, **kwargs):
        raise subscription.ClaudeIdentityUnavailable("Missing account metadata")

    monkeypatch.setattr(subscription, "_claude_account", unidentified)
    info = TestClient(server.app).get("/health").json()["providers"]["clape"]
    assert info["subscription_needs_verification"] is True
    assert not info.get("codex_home_unavailable")



def test_health_keeps_provider_snapshot_during_reload(isolated, monkeypatch):
    claude_provider(monkeypatch)
    subscription.ensure_home("clape", "claudecode")

    def account_during_reload(home, harness):
        assert harness == "claudecode"
        providers.PROVIDERS.pop("clape")
        return subscription.CLAUDE_UNVERIFIED

    monkeypatch.setattr(subscription, "account_identity", account_during_reload)
    client = TestClient(server.app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["providers"]["clape"]["subscription_fresh_only"]
    assert "clape" not in client.get("/health").json()["providers"]
