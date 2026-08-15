"""End-to-end orchestration test for the migrated Idea Generation pipeline, with the
LLM/retrieval boundary faked out (verifies call sequencing, gating, and data flow through
run_full_pipeline; the real Agent/provider streaming internals are main's own concern,
covered by test/unit/test_agent.py).
"""

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.idea_generation import evidence_retrieval, workflow
from athena.research.idea_generation.idea_schemas import (
    FalsifiabilityJudgment,
    GapMiningResponse,
    GateVerdict,
    HypothesisDraft,
    NoveltyEvidenceJudgment,
    PairwiseJudgment,
    ResearchProblemInput,
    RevisionDraft,
    SkepticJudgment,
)


def _fake_hypothesis_draft(strategy_id: str) -> HypothesisDraft:
    return HypothesisDraft(
        statement=f"X causes Y via {strategy_id}",
        intervention="swap absolute position embedding for relative distance bias",
        expected_effect="masked MAE on long sequences drops",
        generation_strategy=strategy_id,
        supported_premises=[
            {
                "claim": "train set skews short, test set has long sequences",
                "role": "supported_premise",
                "supporting_refs": ["ev-0"],
            }
        ],
        inference_chain=[],
        predicted_observations=["dev_long MAE decreases"],
        disconfirming_observations=["dev_long shows no improvement"],
    )


_FAKE_RESPONSES = {
    FalsifiabilityJudgment: lambda: FalsifiabilityJudgment(
        testable_implication="measure dev_long MAE before/after", unobservable_variables=[],
        is_falsifiable=True,
    ),
    GapMiningResponse: lambda: GapMiningResponse(gaps=[]),
    NoveltyEvidenceJudgment: lambda: NoveltyEvidenceJudgment(
        nearest_work=[], facet_overlap={"problem": 0.2, "mechanism": 0.1},
        coverage_estimate=0.6, unrecalled_risk=0.2, citation_cutoff_ok=True,
        retrieval_cutoff_ok=True, post_cutoff_similarity=0.1, possible_memorization=False,
        leakage_risk=0.1, historical_backtest_validity=True, uncertainty=0.2,
    ),
    SkepticJudgment: lambda: SkepticJudgment(
        critique="reasonable", unaddressed_risks=[], fatal_flaw_found=False,
    ),
    RevisionDraft: lambda: RevisionDraft(
        rebuttal="added control", changes_made=["added control arm"],
        revised_novel_hypothesis="X causes Y (revised)", revised_premises=[],
        revised_predicted_observations=["p"], revised_disconfirming_observations=["d"],
    ),
    PairwiseJudgment: lambda: PairwiseJudgment(winner="candidate_a", rationale="more falsifiable"),
}


async def _fake_single_turn_structured_chat(prompt, schema, *, model, artifacts, **kwargs):
    if schema is HypothesisDraft:
        strategy_id = "unknown"
        for line in prompt.splitlines():
            if line.startswith("Generation strategy:"):
                strategy_id = line.split(":", 1)[1].strip()
        return _fake_hypothesis_draft(strategy_id)
    return _FAKE_RESPONSES[schema]()


async def _fake_run_retrieval_agent(agent, question):
    return "no relevant prior work found", ["paper_keyword_search"]


@pytest.fixture(autouse=True)
def _patch_llm_boundary(monkeypatch):
    """Patch every module's own imported reference to the two LLM/retrieval entry
    points (each module holds its own `from X import Y` copy, so the source module's
    attribute alone is not enough)."""
    modules = [
        "athena.research.idea_generation.structured_chat",
        "athena.research.idea_generation.workflow",
        "athena.research.idea_generation.pre_gate_checks",
        "athena.research.idea_generation.review_board",
        "athena.research.idea_generation.evidence_retrieval",
        "athena.research.idea_generation.candidate_generation",
        "athena.research.idea_generation.revision",
    ]
    for module_path in modules:
        monkeypatch.setattr(
            f"{module_path}.single_turn_structured_chat", _fake_single_turn_structured_chat,
        )
    monkeypatch.setattr(evidence_retrieval, "run_retrieval_agent", _fake_run_retrieval_agent)
    import athena.research.idea_generation.review_board as review_board_mod
    monkeypatch.setattr(review_board_mod, "run_retrieval_agent", _fake_run_retrieval_agent)


@pytest.mark.asyncio
async def test_run_full_pipeline_generates_gates_and_ranks_candidates(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    corpus_ref = await store.put_text("fake corpus")
    problem = ResearchProblemInput(
        question="Does relative position encoding improve long-RNA masked MAE?",
        domain="ai4s", objective="lower masked MAE on long sequences",
        constraints=["single GPU 24GB", "<=6h per experiment"],
        evidence_texts=["train set RNA sequences are shorter than the test set"],
    )
    dummy_agent = object()

    results, ranking = await workflow.run_full_pipeline(
        problem, gap_miner_agent=dummy_agent, novelty_agent=dummy_agent,
        domain_review_agent=dummy_agent,
        artifacts=store, corpus_ref=corpus_ref, sample_size=2, model="fake-model",
    )

    assert len(results) == 2
    assert all(r.decision.verdict == GateVerdict.PASS for r in results)
    assert len(ranking) == 2
    assert {rc.idea_id for rc in ranking} == {r.package.idea_id for r in results}
    # Elo ratings actually moved from the 1200 default after one comparison.
    assert any(rc.rating != 1200.0 for rc in ranking)


@pytest.mark.asyncio
async def test_run_full_pipeline_empty_candidates_returns_empty(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path / "artifacts")
    corpus_ref = await store.put_text("fake corpus")
    problem = ResearchProblemInput(
        question="q", domain="d", objective="o", evidence_texts=[],
    )

    async def _empty_generate(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "athena.research.idea_generation.workflow.generate_candidates", _empty_generate,
    )

    results, ranking = await workflow.run_full_pipeline(
        problem, gap_miner_agent=object(), novelty_agent=object(), domain_review_agent=object(),
        artifacts=store, corpus_ref=corpus_ref, sample_size=1, model="fake-model",
    )

    assert results == []
    assert ranking == []
