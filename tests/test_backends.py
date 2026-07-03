import os
from unittest.mock import patch

import pytest

from agent.backends import Backend, DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS, Gauge, _validate_backend
from agent.runners import _codex_config_args


def test_multiple_backends_can_share_driver():
    deepseek = _validate_backend("deepseek", {
        "driver": "claude",
        "color": "#4d9de0",
        "env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
    })
    claude = _validate_backend("claude", {"driver": "claude", "color": "#AE5332"})

    assert deepseek.driver == claude.driver == "claude"
    assert deepseek.id != claude.id
    assert deepseek.color == "#4D9DE0"


def test_backend_protocol_defaults_to_interactive_cli_and_can_select_oneshot():
    claude = _validate_backend("claude", {"driver": "claude"})
    oneshot = _validate_backend("claude-oneshot", {
        "driver": "claude",
        "protocol": "oneshot-cli",
    })

    assert claude.protocol == "interactive-cli"
    assert oneshot.protocol == "oneshot-cli"
    assert claude.fingerprint != oneshot.fingerprint
    assert claude.public_dict()["protocol"] == "interactive-cli"
    assert claude.interactive.idle_timeout_seconds == DEFAULT_INTERACTIVE_IDLE_TIMEOUT_SECONDS


def test_backend_interactive_idle_timeout_is_configurable():
    live = _validate_backend("claude-live", {
        "driver": "claude",
        "protocol": "interactive-cli",
        "interactive": {"idle_timeout_seconds": 8 * 60 * 60},
    })

    assert live.interactive.idle_timeout_seconds == 28800
    assert live.public_dict()["interactive"]["idle_timeout_seconds"] == 28800


def test_backend_rejects_invalid_interactive_idle_timeout():
    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        _validate_backend("claude-live", {
            "driver": "claude",
            "protocol": "interactive-cli",
            "interactive": {"idle_timeout_seconds": -1},
        })


def test_backend_accepts_codex_interactive_cli_protocol():
    backend = _validate_backend("codex-live", {
        "driver": "codex",
        "protocol": "interactive-cli",
    })

    assert backend.protocol == "interactive-cli"


def test_backend_accepts_cursor_interactive_cli_protocol():
    backend = _validate_backend("cursor-live", {
        "driver": "cursor",
        "protocol": "interactive-cli",
    })

    assert backend.protocol == "interactive-cli"


def test_backend_accepts_opencode_interactive_cli_protocol():
    backend = _validate_backend("opencode-live", {
        "driver": "opencode",
        "protocol": "interactive-cli",
    })

    assert backend.protocol == "interactive-cli"


def test_backend_secret_reference_is_resolved_at_execution_time():
    backend = Backend(
        "deepseek", "claude", env={"ANTHROPIC_AUTH_TOKEN": {"env": "DEEPSEEK_API_KEY"}}
    )
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret"}):
        assert backend.resolved_env() == {"ANTHROPIC_AUTH_TOKEN": "secret"}
        assert backend.missing_secrets() == []


def test_backend_reports_missing_secret_without_exposing_it():
    backend = Backend(
        "deepseek", "claude", env={"ANTHROPIC_AUTH_TOKEN": {"env": "MISSING_SQUID_TEST_KEY"}}
    )
    with patch.dict(os.environ, {}, clear=True):
        assert backend.missing_secrets() == ["MISSING_SQUID_TEST_KEY"]
        with pytest.raises(ValueError, match="MISSING_SQUID_TEST_KEY"):
            backend.resolved_env()


def test_codex_settings_are_flattened_into_config_arguments():
    args = _codex_config_args({
        "model_provider": "vllm_mlx",
        "model_providers": {
            "vllm_mlx": {
                "base_url": "http://127.0.0.1:9000/v1",
                "wire_api": "responses",
            }
        },
    })

    assert args == [
        "-c", 'model_provider="vllm_mlx"',
        "-c", 'model_providers.vllm_mlx.base_url="http://127.0.0.1:9000/v1"',
        "-c", 'model_providers.vllm_mlx.wire_api="responses"',
    ]


def test_canonical_connection_fields_translate_for_claude():
    backend = _validate_backend("deepseek", {
        "driver": "claude",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key": "deepseek-secret",
        "gauge": "deepseek",
    })

    assert backend.execution_env() == {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "deepseek-secret",
    }
    assert backend.gauge == Gauge(type="deepseek")


def test_deepseek_backend_without_api_key_is_not_available():
    backend = _validate_backend("deepseek", {
        "driver": "claude",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/anthropic",
        "gauge": "deepseek",
    })

    with patch("agent.backends._DRIVER_PATHS", {"claude": "/usr/local/bin/claude"}):
        assert backend.missing_secrets() == ["api_key"]
        assert backend.public_dict()["missing_secrets"] == ["api_key"]
        assert backend.public_dict()["missing_requirements"] == ["api_key"]
        assert backend.available is False


def test_provider_backend_without_base_url_is_not_available():
    backend = _validate_backend("qwen", {
        "driver": "codex",
        "provider": "qwen",
        "gauge": {"type": "static", "text": "Local"},
    })

    with patch("agent.backends._DRIVER_PATHS", {"codex": "/usr/local/bin/codex"}):
        assert backend.kind == "provider"
        assert backend.missing_settings() == ["base_url"]
        assert backend.public_dict()["missing_requirements"] == ["base_url"]
        assert backend.available is False


def test_deepseek_backend_without_base_url_is_not_available():
    backend = _validate_backend("deepseek", {
        "driver": "claude",
        "provider": "deepseek",
        "api_key": "deepseek-secret",
        "gauge": "deepseek",
    })

    with patch("agent.backends._DRIVER_PATHS", {"claude": "/usr/local/bin/claude"}):
        assert backend.missing_settings() == ["base_url"]
        assert backend.public_dict()["missing_requirements"] == ["base_url"]
        assert backend.available is False


def test_deepseek_backend_with_api_key_can_be_available():
    backend = _validate_backend("deepseek", {
        "driver": "claude",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key": "deepseek-secret",
        "gauge": "deepseek",
    })

    with patch("agent.backends._DRIVER_PATHS", {"claude": "/usr/local/bin/claude"}):
        assert backend.kind == "provider"
        assert backend.missing_secrets() == []
        assert backend.missing_settings() == []
        assert backend.available is True


def test_canonical_connection_fields_translate_for_codex():
    backend = _validate_backend("qwen", {
        "driver": "codex",
        "provider": "qwen",
        "base_url": "http://127.0.0.1:9000/v1",
        "api_key": {"env": "QWEN_API_KEY"},
        "gauge": {"type": "static", "text": "Local"},
    })

    with patch.dict(os.environ, {"QWEN_API_KEY": "local-secret"}):
        assert backend.execution_env()["SQUID_BACKEND_API_KEY"] == "local-secret"
    assert backend.driver_settings() == {
        "model_provider": "qwen",
        "model_providers": {
            "qwen": {
                "name": "qwen",
                "base_url": "http://127.0.0.1:9000/v1",
                "wire_api": "responses",
                "env_key": "SQUID_BACKEND_API_KEY",
            }
        },
    }


def test_deepseek_opencode_backend_uses_its_own_api_key():
    backend = _validate_backend("deepopen", {
        "driver": "opencode", "provider": "deepseek",
        "api_key": "separate-key", "gauge": "deepseek",
    })

    assert backend.execution_env()["DEEPSEEK_API_KEY"] == "separate-key"


def test_static_gauge_requires_display_text():
    with pytest.raises(ValueError, match="requires text"):
        _validate_backend("local", {"driver": "codex", "gauge": {"type": "static"}})


@pytest.mark.parametrize("color", ["red", "#123", "#12345678", "#GGGGGG"])
def test_invalid_backend_color_is_rejected(color):
    with pytest.raises(ValueError, match="color"):
        _validate_backend("test", {"driver": "codex", "color": color})
