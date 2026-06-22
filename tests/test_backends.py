import os
from unittest.mock import patch

import pytest

from agent.backends import Backend, Gauge, _validate_backend
from agent.runners import _codex_config_args


def test_multiple_backends_can_share_driver():
    deepcla = _validate_backend("deepcla", {
        "driver": "claude",
        "color": "#4d9de0",
        "env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
    })
    claude = _validate_backend("claude", {"driver": "claude", "color": "#AE5332"})

    assert deepcla.driver == claude.driver == "claude"
    assert deepcla.id != claude.id
    assert deepcla.color == "#4D9DE0"


def test_backend_secret_reference_is_resolved_at_execution_time():
    backend = Backend(
        "deepcla", "claude", env={"ANTHROPIC_AUTH_TOKEN": {"env": "DEEPSEEK_API_KEY"}}
    )
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret"}):
        assert backend.resolved_env() == {"ANTHROPIC_AUTH_TOKEN": "secret"}
        assert backend.missing_secrets() == []


def test_backend_reports_missing_secret_without_exposing_it():
    backend = Backend(
        "deepcla", "claude", env={"ANTHROPIC_AUTH_TOKEN": {"env": "MISSING_SQUID_TEST_KEY"}}
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
    backend = _validate_backend("deepcla", {
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
