from agent.journal import _build_prompt


def test_journal_prompt_includes_handoff_instructions():
    prompt = _build_prompt(
        topic="squid",
        agent="codex",
        week_key="2026-W22",
        week_start="2026-05-25T00:00:00Z",
        week_end="2026-06-01T00:00:00Z",
        messages=[
            {"role": "user", "content": "fix token counting"},
            {"role": "assistant", "content": "updated runners.py"},
        ],
    )

    assert "future agent's input as context" in prompt
    assert "## How to use this" in prompt
    assert "what context to trust" in prompt
