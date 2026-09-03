"""Prompt requirements for researched baseline design and provenance."""

from athena.agents.prompt_agent import load_prompt


def test_baseline_ideator_prompt_requires_research_and_policy_artifacts() -> None:
    prompt = load_prompt("baseline_ideator")

    for text in (
        "web_search",
        "web_fetch",
        "BASELINE_RESEARCH.json",
        "BASELINE_DESIGN.md",
        "Selected candidate:",
        "Training strategy:",
        "effective training units",
        "frozen_pretrained",
        "partial_finetune",
        "train_from_scratch",
        '"dataset"',
        '"decisions"',
        '"source_kind"',
        '"repository_url"',
        "independently verified OpenAlex",
        "official scikit-learn",
    ):
        assert text in prompt
    assert "Do not execute" in prompt


def test_prepare_prompt_requires_platform_verification_provenance() -> None:
    prompt = load_prompt("prepare")

    for text in (
        "BASELINE_RESEARCH_VERIFICATION.json",
        "validated source",
        "RESEARCH_HANDOFF.md",
        "training strategy",
    ):
        assert text in prompt
