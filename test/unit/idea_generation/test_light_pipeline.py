"""End-to-end test for gate.run_light_pipeline: pre_gate + methodology/statistics review +
light_hard_gate, with the LLM boundary faked out."""

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.idea_generation import gate as gate_module
from athena.research.idea_generation.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    IdeatorHypothesisDraft,
    SkepticJudgment,
)


def _draft(statement: str) -> IdeatorHypothesisDraft:
    return IdeatorHypothesisDraft(
        statement=statement,
        intervention="add a Cabin-deck feature",
        expected_effect="validation accuracy increases",
        supported_premises=[
            ClaimEvidence(
                claim="Cabin letter correlates with fare class in the EDA report",
                role=ClaimRole.SUPPORTED_PREMISE,
                supporting_refs=["eda-report"],
            )
        ],
        predicted_observations=["accuracy increases"],
        disconfirming_observations=["accuracy stays flat or drops"],
    )


_FAKE_RESPONSES = {
    FalsifiabilityJudgment: lambda: FalsifiabilityJudgment(
        testable_implication="measure validation accuracy before/after",
        unobservable_variables=[],
        is_falsifiable=True,
    ),
    SkepticJudgment: lambda: SkepticJudgment(
        critique="reasonable",
        unaddressed_risks=[],
        fatal_flaw_found=False,
    ),
}


async def _fake_chat(prompt, schema, *, model, artifacts, **kwargs):
    return _FAKE_RESPONSES[schema]()


@pytest.fixture(autouse=True)
def _patch_llm(monkeypatch):
    for module_path in (
        "athena.research.idea_generation.gate",
        "athena.research.idea_generation.review_board",
    ):
        monkeypatch.setattr(f"{module_path}.single_turn_structured_chat", _fake_chat)


@pytest.mark.asyncio
async def test_run_light_pipeline_keeps_passing_drafts_in_submission_order(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    drafts = [
        _draft("Cabin deck predicts survival"),
        _draft("Ticket group size predicts survival"),
    ]

    kept = await gate_module.run_light_pipeline(
        drafts, model="fake-model", artifacts=store
    )

    assert [h.statement for h in kept] == [d.statement for d in drafts]
    assert all(h.intervention == "add a Cabin-deck feature" for h in kept)
    assert all(h.evidence_refs == ["eda-report"] for h in kept)


@pytest.mark.asyncio
async def test_run_light_pipeline_drops_unfalsifiable_draft(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path / "artifacts")
    drafts = [_draft("A"), _draft("B")]

    calls = {"n": 0}

    async def _mixed_chat(prompt, schema, *, model, artifacts, **kwargs):
        if schema is FalsifiabilityJudgment:
            calls["n"] += 1
            if calls["n"] == 1:
                return FalsifiabilityJudgment(
                    testable_implication="",
                    unobservable_variables=["true cause unknown"],
                    is_falsifiable=False,
                )
        return _FAKE_RESPONSES[schema]()

    monkeypatch.setattr(gate_module, "single_turn_structured_chat", _mixed_chat)

    kept = await gate_module.run_light_pipeline(
        drafts, model="fake-model", artifacts=store
    )

    assert len(kept) == 1
    assert kept[0].statement == "B"


@pytest.mark.asyncio
async def test_run_light_pipeline_empty_input_returns_empty(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    kept = await gate_module.run_light_pipeline([], model="fake-model", artifacts=store)
    assert kept == []
