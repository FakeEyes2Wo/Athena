"""语料开关：有 corpus_ref 走完整 hard_gate（含 novelty + domain_consistency），
没有就走 light_hard_gate。一个开关，优雅降级。

对应 Corpus-Grounded Ideation（feature/academic-survey, e1ab4d8）的 ``--survey`` 默认关闭
哲学：语料是可选能力，缺它时整条链路仍然完整可跑，只是少了新颖性这一维。
"""

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.idea_generation import gate as gate_module
from athena.research.idea_generation.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    NoveltyEvidenceJudgment,
    PairwiseJudgment,
    IdeatorHypothesisDraft,
    SkepticJudgment,
)

_CORPUS = "sha256:" + "c" * 64


def _draft(statement: str = "Cabin deck predicts survival") -> IdeatorHypothesisDraft:
    return IdeatorHypothesisDraft(
        statement=statement, intervention="add Cabin-deck feature",
        expected_effect="accuracy increases",
        supported_premises=[
            ClaimEvidence(
                claim="deck correlates with fare class", role=ClaimRole.SUPPORTED_PREMISE,
                supporting_refs=["eda-report"],
            )
        ],
        predicted_observations=["accuracy increases"],
        disconfirming_observations=["accuracy stays flat"],
        sources=["arxiv:2401.00001"],
    )


def _fake_response(schema, *, overlap: float = 0.1):
    if schema is FalsifiabilityJudgment:
        return FalsifiabilityJudgment(
            testable_implication="measure accuracy", unobservable_variables=[],
            is_falsifiable=True,
        )
    if schema is SkepticJudgment:
        return SkepticJudgment(critique="fine", unaddressed_risks=[], fatal_flaw_found=False)
    if schema is NoveltyEvidenceJudgment:
        return NoveltyEvidenceJudgment(
            nearest_work=[], facet_overlap={"problem": overlap, "mechanism": overlap},
            coverage_estimate=0.6, unrecalled_risk=0.2, citation_cutoff_ok=True,
            retrieval_cutoff_ok=True, post_cutoff_similarity=0.1, possible_memorization=False,
            leakage_risk=0.1, historical_backtest_validity=True, uncertainty=0.2,
        )
    if schema is PairwiseJudgment:
        return PairwiseJudgment(winner="candidate_a", rationale="clearer")
    raise AssertionError(f"unexpected schema {schema}")


def _patch(monkeypatch, *, overlap: float = 0.1):
    async def _fake_chat(prompt, schema, *, model, artifacts, **kwargs):
        return _fake_response(schema, overlap=overlap)

    async def _fake_retrieval(agent, question):
        return "retrieval transcript", ["paper_keyword_search"]

    for module_path in (
        "athena.research.idea_generation.gate",
        "athena.research.idea_generation.pre_gate_checks",
        "athena.research.idea_generation.review_board",
        "athena.research.idea_generation.evidence_retrieval",
    ):
        monkeypatch.setattr(f"{module_path}.single_turn_structured_chat", _fake_chat)
    for module_path in (
        "athena.research.idea_generation.evidence_retrieval",
        "athena.research.idea_generation.review_board",
    ):
        monkeypatch.setattr(f"{module_path}.run_retrieval_agent", _fake_retrieval)


@pytest.mark.asyncio
async def test_without_corpus_novelty_is_never_consulted(tmp_path, monkeypatch):
    """没有语料时不得调用检索——否则等于假装做了新颖性审计。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    _patch(monkeypatch)

    called = {"retrieval": 0}

    async def _tripwire(agent, question):
        called["retrieval"] += 1
        return "", []

    monkeypatch.setattr(
        "athena.research.idea_generation.evidence_retrieval.run_retrieval_agent", _tripwire
    )

    kept = await gate_module.run_light_pipeline([_draft()], model="fake-model", artifacts=store)

    assert len(kept) == 1
    assert called["retrieval"] == 0


@pytest.mark.asyncio
async def test_with_corpus_runs_novelty_and_still_passes_a_novel_candidate(
    tmp_path, monkeypatch
):
    """有语料时真的跑 novelty 审计；重叠度低的候选照常通过。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    _patch(monkeypatch, overlap=0.1)
    agent = object()

    kept = await gate_module.run_light_pipeline(
        [_draft()], model="fake-model", artifacts=store,
        corpus_ref=_CORPUS, novelty_agent=agent, domain_review_agent=agent,
    )

    assert len(kept) == 1


@pytest.mark.asyncio
async def test_with_corpus_rejects_a_candidate_that_duplicates_prior_work(
    tmp_path, monkeypatch
):
    """重叠度超阈值 -> hard_gate 判 REJECT -> 不入库。这正是语料带来的新能力。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    _patch(monkeypatch, overlap=0.95)
    agent = object()

    kept = await gate_module.run_light_pipeline(
        [_draft()], model="fake-model", artifacts=store,
        corpus_ref=_CORPUS, novelty_agent=agent, domain_review_agent=agent,
    )

    assert kept == []


@pytest.mark.asyncio
async def test_corpus_ref_without_agents_is_rejected(tmp_path):
    """半配置是装配错误，不能静默降级成"没有语料"——那会悄悄丢掉新颖性门槛。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError, match="novelty_agent"):
        await gate_module.run_light_pipeline(
            [_draft()], model="fake-model", artifacts=store, corpus_ref=_CORPUS,
        )
