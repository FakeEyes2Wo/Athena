"""Corpus-grounded ideation handoff: the papers the Ideator actually read travel with the
candidate, so the adversarial audit stages spend their retrieval budget on what the
generator missed rather than rediscovering what it already found.
"""

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.research.idea_generation import gate as gate_module
from athena.research.idea_generation.evidence_retrieval import build_novelty_question
from athena.research.idea_generation.idea_schemas import (
    ClaimEvidence,
    ClaimRole,
    FalsifiabilityJudgment,
    HypothesisPackage,
    IdeatorHypothesisDraft,
    PairwiseJudgment,
    RevisionDraft,
    SkepticJudgment,
)
from athena.research.idea_generation.revision import revise_candidate

_PAPERS = ["arxiv:2401.00001", "arxiv:2402.00002"]


def _package(**overrides) -> HypothesisPackage:
    defaults = dict(
        idea_id="idea-1", generation_strategy="eda_grounded",
        novel_hypothesis="X causes Y", supported_premises=[], inference_chain=[],
        predicted_observations=["p"], disconfirming_observations=["d"],
        lineage_op="generate", sources=list(_PAPERS),
    )
    defaults.update(overrides)
    return HypothesisPackage(**defaults)


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
        sources=list(_PAPERS),
    )


# ====== sources 贯通 ======

def test_package_defaults_to_no_sources_when_corpus_is_off():
    """语料关闭时（现状）行为不变：sources 缺省为空，不是必填。"""
    package = HypothesisPackage(
        idea_id="idea-1", generation_strategy="eda_grounded", novel_hypothesis="X",
        supported_premises=[], inference_chain=[], predicted_observations=["p"],
        disconfirming_observations=["d"], lineage_op="generate",
    )
    assert package.sources == []


def test_ideator_draft_sources_reach_the_core_hypothesis():
    """Ideator 真正读过的论文要落到 core.Hypothesis.sources，供下游与审计共用。"""
    draft = _draft()
    package = gate_module._build_package(draft)
    assert package.sources == _PAPERS

    hypothesis = gate_module._to_core_hypothesis(draft, package)
    assert hypothesis.sources == _PAPERS


@pytest.mark.asyncio
async def test_revision_carries_sources_through(tmp_path, monkeypatch):
    """修订闭环不改变"读过哪些论文"这一事实，sources 必须原样穿过修订。

    否则终局刷新时 novelty 会因为 package.sources 变空而丢掉接力上下文，
    白白重新检索一遍。
    """
    store = LocalArtifactStore(tmp_path / "artifacts")

    async def _fake_chat(prompt, schema, *, model, artifacts, **kwargs):
        return RevisionDraft(
            rebuttal="added control", changes_made=["added control"],
            revised_novel_hypothesis="X causes Y (revised)", revised_premises=[],
            revised_predicted_observations=["p2"], revised_disconfirming_observations=["d2"],
        )

    monkeypatch.setattr(
        "athena.research.idea_generation.revision.single_turn_structured_chat", _fake_chat
    )

    revised, _draft_out = await revise_candidate(
        _package(), blocking_factor="risk_ok_methodology",
        debated_perspective="methodology", reviews=[], prior_rounds=[], artifacts=store,
        model="fake-model",
    )

    assert revised.sources == _PAPERS


# ====== 接力：novelty 提问带上生成侧已考虑过的论文 ======

def test_novelty_question_lists_papers_the_generator_already_used():
    """审计预算要花在"生成侧漏了什么"，所以提问里必须点明哪些已经被看过。"""
    question = build_novelty_question(_package(), "sha256:" + "a" * 64)
    for paper in _PAPERS:
        assert paper in question


def test_novelty_question_without_sources_says_so_explicitly():
    """没有生成侧上下文时要明确说明，而不是留一段空白让模型自己猜。"""
    question = build_novelty_question(_package(sources=[]), "sha256:" + "a" * 64)
    assert "none" in question.lower()


def test_novelty_question_is_a_stable_staleness_fingerprint():
    """同样输入必须逐字节一致——它是 novelty_is_stale 的指纹来源。"""
    corpus_ref = "sha256:" + "a" * 64
    assert build_novelty_question(_package(), corpus_ref) == build_novelty_question(
        _package(), corpus_ref
    )


def test_novelty_question_changes_when_generator_sources_change():
    """接力上下文变了，指纹就该变，让缓存的旧报告正确失效。"""
    corpus_ref = "sha256:" + "a" * 64
    assert build_novelty_question(_package(), corpus_ref) != build_novelty_question(
        _package(sources=["arxiv:9999.99999"]), corpus_ref
    )


# ====== 端到端：sources 穿过 run_light_pipeline ======

_FAKE = {
    FalsifiabilityJudgment: lambda: FalsifiabilityJudgment(
        testable_implication="measure accuracy", unobservable_variables=[], is_falsifiable=True,
    ),
    SkepticJudgment: lambda: SkepticJudgment(
        critique="fine", unaddressed_risks=[], fatal_flaw_found=False,
    ),
    PairwiseJudgment: lambda: PairwiseJudgment(winner="candidate_a", rationale="clearer"),
}


@pytest.mark.asyncio
async def test_light_pipeline_preserves_sources_end_to_end(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path / "artifacts")

    async def _fake_chat(prompt, schema, *, model, artifacts, **kwargs):
        return _FAKE[schema]()

    for module_path in (
        "athena.research.idea_generation.gate",
        "athena.research.idea_generation.pre_gate_checks",
        "athena.research.idea_generation.review_board",
    ):
        monkeypatch.setattr(f"{module_path}.single_turn_structured_chat", _fake_chat)

    kept = await gate_module.run_light_pipeline(
        [_draft()], model="fake-model", artifacts=store
    )

    assert len(kept) == 1
    assert kept[0].sources == _PAPERS
