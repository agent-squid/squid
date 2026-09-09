from agent.providers import Provider
from agent.resolve import ResolvedAgent


def _codex_base_url(base_url: str) -> str:
    provider = Provider(
        id="omlx",
        base_url=base_url,
        supported_apis=frozenset({"/v1/messages", "/v1/responses"}),
    )
    settings = ResolvedAgent("codex", provider, "oneshot-cli").harness_settings()
    return settings["model_providers"]["omlx"]["base_url"]


def test_codex_adds_responses_api_parent_to_provider_root():
    assert _codex_base_url("http://omlx:8080") == "http://omlx:8080/v1"


def test_codex_adds_responses_api_parent_after_trailing_slash():
    assert _codex_base_url("http://omlx:8080/") == "http://omlx:8080/v1"


def test_codex_does_not_duplicate_existing_responses_api_parent():
    assert _codex_base_url("http://omlx:8080/v1") == "http://omlx:8080/v1"
