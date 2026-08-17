"""Framework ownership constraint must be visible in worker prompts."""

import pytest

from athena.agents.prompt_agent import load_prompt


@pytest.mark.parametrize(
    "agent_type", ["supervisor", "general", "prepare", "evaluator"]
)
def test_worker_prompts_declare_framework_ownership(agent_type: str) -> None:
    prompt = load_prompt(agent_type)

    assert "Framework-owned state" in prompt
    assert ".athena/" in prompt
    assert "Never create, overwrite, or edit anything under" in prompt
    assert "SOTA" in prompt


def test_supervisor_prompt_forbids_delegating_framework_writes() -> None:
    prompt = load_prompt("supervisor")

    assert "Never instruct workers to write framework state" in prompt
