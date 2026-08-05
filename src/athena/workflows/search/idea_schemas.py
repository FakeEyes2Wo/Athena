"""Idea Generation 全流程（步骤 [2]-[9]）专用的结构化事实模型。

这些模型只服务本模块，不进入 core/schemas.py 的跨平面共享模型集合；所有字段的 description
与后续 Prompt 均使用英文。

一条贯穿本文件的约定：任何可行性/新颖性/可证伪性判断的 **verdict 只在 gatekeeper 里产出**
（pre_gate 或 hard_gate），所以除 GateDecision 外，本文件所有报告类模型都不带 verdict 字段，
只承载可审计的事实与逐项打分。另一条约定：凡是 ``XxxJudgment`` 都是"由 LLM 撰写的子集"，
对应的 ``XxxReport`` 才是最终对象，其中 idea_id / 各种 ArtifactRef 一律由代码分配，不向 LLM
索要，避免幻觉 id。
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from athena.core.schemas import ArtifactRef


# ====== 常量 ======

GATE_RUBRIC_VERSION: str = "gate-rubric/v2"

FACETS: tuple[str, ...] = ("problem", "mechanism", "method", "data", "experiment", "conclusion")
"""Facets NoveltyEvidenceCollector scores overlap against; see design doc §8/9.3."""


# ====== 枚举 ======

class ClaimRole(str, Enum):
    """Layered role of a claim inside a hypothesis package."""
    SUPPORTED_PREMISE = "supported_premise"
    DERIVED_INFERENCE = "derived_inference"
    NOVEL_HYPOTHESIS = "novel_hypothesis"
    PREDICTION = "prediction"
    DISCONFIRMER = "disconfirming_observation"


class GateVerdict(str, Enum):
    """Gate verdict values. pre_gate only ever produces PASS or REVISE; hard_gate can also
    produce REJECT (not fixable by revision) and EXPLORATORY (no verifier available)."""
    PASS = "PASS"
    REVISE = "REVISE"
    REJECT = "REJECT"
    EXPLORATORY = "EXPLORATORY"


# ====== 数据模型 ======

class ClaimEvidence(BaseModel):
    """One claim plus its layered role; supported premises must cite evidence.

    Example:
        >>> ClaimEvidence(claim="X causes Y", role=ClaimRole.SUPPORTED_PREMISE,
        ...                supporting_refs=["ev-0"]).role
        <ClaimRole.SUPPORTED_PREMISE: 'supported_premise'>
    """
    claim: str = Field(description="Claim text.")
    role: ClaimRole = Field(description="Layered role of this claim.")
    supporting_refs: list[str] = Field(default_factory=list, description="Evidence ref ids.")

    @model_validator(mode="after")
    def check_layered_rule(self) -> "ClaimEvidence":
        # 分层规则：支撑前提必须绑证据；新假设本身不得直接带证据（否则不算"新"）
        if self.role == ClaimRole.SUPPORTED_PREMISE and not self.supporting_refs:
            raise ValueError("supported premise must bind evidence refs")
        if self.role == ClaimRole.NOVEL_HYPOTHESIS and self.supporting_refs:
            raise ValueError("novel hypothesis must not carry direct evidence refs")
        return self


class InferenceStep(BaseModel):
    """One explicit inference step in the reasoning chain.

    Example:
        >>> InferenceStep(step_id="s1", from_premises=["p1"], operator="mechanistic",
        ...                to_claim="c1", uncertainty=0.3).operator
        'mechanistic'
    """
    step_id: str = Field(description="Step id.")
    from_premises: list[str] = Field(description="Upstream claim ids.")
    operator: str = Field(description="analogy | mechanistic | statistical | ...")
    to_claim: str = Field(description="Derived claim.")
    uncertainty: float = Field(ge=0, le=1, description="Subjective uncertainty in [0,1].")


class HypothesisDraft(BaseModel):
    """LLM-authored subset of a hypothesis package; idea_id/lineage_op are assigned by code,
    not requested from the LLM, so they can't be hallucinated or collide.

    Example:
        >>> HypothesisDraft(statement="s", intervention="i", expected_effect="e",
        ...                  generation_strategy="single_strategy_v1", supported_premises=[],
        ...                  inference_chain=[], predicted_observations=["p"],
        ...                  disconfirming_observations=["d"]).statement
        's'
    """
    statement: str = Field(description="Falsifiable hypothesis statement (short).")
    intervention: str = Field(description="Minimal change or observation to test it.")
    expected_effect: str = Field(description="Expected measurable effect.")
    generation_strategy: str = Field(description="Strategy that produced this candidate.")
    sampling_probability: float = Field(
        default=1.0, ge=0, le=1,
        description="Self-assessed probability from Verbalized Sampling that this candidate is "
                    "worth pursuing; defaults to 1.0 for single-candidate (non-sampled) generation.",
    )
    supported_premises: list[ClaimEvidence] = Field(description="Evidence-bound premises.")
    inference_chain: list[InferenceStep] = Field(description="Explicit reasoning steps.")
    predicted_observations: list[str] = Field(description="Observations predicted if true.")
    disconfirming_observations: list[str] = Field(description="Observations that would refute it.")

    @model_validator(mode="after")
    def check_novel_hypothesis_testability(self) -> "HypothesisDraft":
        # 新假设必须带预测与反证条件，否则在结构门槛这一步就该 REVISE
        if not self.predicted_observations or not self.disconfirming_observations:
            raise ValueError("novel hypothesis requires predictions and disconfirmers")
        return self


class HypothesisPackage(BaseModel):
    """Full structured hypothesis package; the module-local audit object for one candidate.

    Example:
        >>> HypothesisPackage(idea_id="idea-1", generation_strategy="single_strategy_v1",
        ...                    novel_hypothesis="n", supported_premises=[], inference_chain=[],
        ...                    predicted_observations=["p"], disconfirming_observations=["d"],
        ...                    lineage_op="generate").idea_id
        'idea-1'
    """
    idea_id: str = Field(description="Same id as the Hypothesis node.")
    generation_strategy: str = Field(description="Strategy that produced this candidate.")
    sampling_probability: float = Field(
        default=1.0, ge=0, le=1,
        description="Self-assessed probability carried over from the generating HypothesisDraft.",
    )
    novel_hypothesis: str = Field(description="The novel claim under test.")
    supported_premises: list[ClaimEvidence] = Field(description="Evidence-bound premises.")
    inference_chain: list[InferenceStep] = Field(description="Explicit reasoning steps.")
    predicted_observations: list[str] = Field(description="Observations predicted if true.")
    disconfirming_observations: list[str] = Field(description="Observations that would refute it.")
    validation_plan_ref: ArtifactRef | None = Field(
        default=None,
        description="ValidationPlan artifact ref; stays None until validation planning.",
    )
    revision_round: int = Field(
        default=0, ge=0,
        description="How many debate revisions produced this package; 0 for an original candidate.",
    )
    lineage_op: str = Field(description="generate | specialize | merge | mutate | branch.")

    @model_validator(mode="after")
    def check_novel_hypothesis_testability(self) -> "HypothesisPackage":
        # 与 HypothesisDraft 相同的不变量；在最终落盘对象上再校验一次，保证审计对象自身自洽
        if not self.predicted_observations or not self.disconfirming_observations:
            raise ValueError("novel hypothesis requires predictions and disconfirmers")
        return self


class ResearchProblemInput(BaseModel):
    """问题定格输入（步骤 [1]）。背景证据目前仍以文本列表手工传入——设计文档里"证据侧改用
    paper_rag 检索 Agent"只落到了步骤 [2]/[5]，步骤 [1] 的证据自动化留给后续迭代。

    Example:
        >>> ResearchProblemInput(question="q", domain="d", objective="o",
        ...                       evidence_texts=["fact"]).evidence_texts
        ['fact']
    """
    question: str = Field(description="Research question in natural language.")
    domain: str = Field(description="Scientific domain.")
    objective: str = Field(description="What a successful hypothesis should achieve.")
    constraints: list[str] = Field(default_factory=list, description="Hard constraints / forbidden areas.")
    evidence_texts: list[str] = Field(
        default_factory=list, description="Manually supplied background evidence, one string per item."
    )


class StructuralCheckReport(BaseModel):
    """Pure-function structural check result; not a verdict (judgment stays with the gatekeeper).

    Example:
        >>> StructuralCheckReport(idea_id="idea-1", premise_evidence_ok=True,
        ...                        novel_hypothesis_testable=True).violations
        []
    """
    idea_id: str = Field(description="Candidate id.")
    premise_evidence_ok: bool = Field(description="Every SUPPORTED_PREMISE has bound evidence refs.")
    novel_hypothesis_testable: bool = Field(
        description="Novel hypothesis carries predictions and disconfirmers."
    )
    violations: list[str] = Field(default_factory=list, description="Failed rule ids; empty if ok.")


class FalsifiabilityJudgment(BaseModel):
    """LLM-authored subset of a falsifiability report; idea_id is assigned by code.

    Example:
        >>> FalsifiabilityJudgment(testable_implication="t", unobservable_variables=[],
        ...                         is_falsifiable=True).is_falsifiable
        True
    """
    testable_implication: str = Field(default="", description="The testable implication found, if any.")
    unobservable_variables: list[str] = Field(
        default_factory=list, description="Variables that block observability."
    )
    is_falsifiable: bool = Field(description="Whether a testable implication was designed.")

    @model_validator(mode="after")
    def check_falsifiable_has_evidence(self) -> "FalsifiabilityJudgment":
        # gatekeeper 会把 testable_implication 原文当作 falsifiable 这一项的 evidence；
        # 若 is_falsifiable=True 却允许 testable_implication 为空，PASS 就可能带空证据，
        # 违反"任何判断都必须绑定可审计证据"的规则，故在此收紧不变量。
        if self.is_falsifiable and not self.testable_implication.strip():
            raise ValueError("falsifiable judgment requires a non-empty testable_implication")
        return self


class FalsifiabilityReport(BaseModel):
    """Design-time falsifiability check result; not a verdict.

    Example:
        >>> FalsifiabilityReport(idea_id="idea-1", testable_implication="t",
        ...                        unobservable_variables=[], is_falsifiable=True).idea_id
        'idea-1'
    """
    idea_id: str = Field(description="Candidate id.")
    testable_implication: str = Field(default="", description="The testable implication found, if any.")
    unobservable_variables: list[str] = Field(
        default_factory=list, description="Variables that block observability."
    )
    is_falsifiable: bool = Field(description="Whether a testable implication was designed.")

    @model_validator(mode="after")
    def check_falsifiable_has_evidence(self) -> "FalsifiabilityReport":
        # 与 FalsifiabilityJudgment 相同的不变量；在最终落盘对象上再校验一次，保证审计对象自身自洽
        if self.is_falsifiable and not self.testable_implication.strip():
            raise ValueError("falsifiable report requires a non-empty testable_implication")
        return self


class RubricItemScore(BaseModel):
    """Per-item rubric score with mandatory evidence (no bare totals).

    Example:
        >>> RubricItemScore(item="evidence_traceable", score=1.0,
        ...                  evidence="all premises cite refs").score
        1.0
    """
    item: str = Field(description="Rubric item id.")
    score: float = Field(description="Item score.")
    evidence: str = Field(description="Concrete evidence for this item score.")
    evidence_refs: list[str] = Field(default_factory=list, description="Evidence ref ids.")


class GateDecision(BaseModel):
    """Decision produced by pre_gate or hard_gate; the only models in this module carrying a verdict.

    Example:
        >>> GateDecision(idea_id="idea-1", gate_phase="pre_gate", verdict=GateVerdict.PASS,
        ...                rubric_version=GATE_RUBRIC_VERSION, item_scores=[]).verdict
        <GateVerdict.PASS: 'PASS'>
    """
    idea_id: str = Field(description="Candidate id.")
    gate_phase: Literal["pre_gate", "full"] = Field(
        description="pre_gate produces this in phase 'pre_gate'; hard_gate produces it in phase 'full'."
    )
    verdict: GateVerdict = Field(description="Gate verdict.")
    rubric_version: str = Field(description="Versioned rubric used, e.g. gate-rubric/v2.")
    item_scores: list[RubricItemScore] = Field(
        description="Per-item scores with evidence: pre_gate always has 2 items "
                    "(evidence_traceable, falsifiable); hard_gate has 5 + one per review "
                    "perspective (adds novelty_ok, verifier_ok, risk_total, and one "
                    "risk_ok_<perspective> per entry in REVIEW_PERSPECTIVES)."
    )
    blocking_factor: str | None = Field(default=None, description="First blocking item if any.")


# ====== 空白挖掘（ResearchGapMiner，步骤 [2]） ======

class GapCandidateDraft(BaseModel):
    """LLM-authored subset of a GapCandidate; gap_id/context_ref are assigned by code.

    Example:
        >>> GapCandidateDraft(gap_type="open_problem", description="d").gap_type
        'open_problem'
    """
    gap_type: Literal["open_problem", "contradiction", "missing_link"] = Field(
        description="Kind of literature gap found."
    )
    description: str = Field(description="Concrete description grounded in retrieved evidence.")


class GapMiningResponse(BaseModel):
    """LLM output wrapper for one ResearchGapMiner run; an empty list is a valid outcome.

    Example:
        >>> GapMiningResponse().gaps
        []
    """
    gaps: list[GapCandidateDraft] = Field(
        default_factory=list,
        description="Zero or more gaps found; empty means no gap was found, not an error.",
    )


class GapCandidate(BaseModel):
    """Final literature gap record; gap_id/context_ref are assigned by code, not the LLM.

    Example:
        >>> GapCandidate(gap_id="gap-1", gap_type="open_problem",
        ...                context_ref="sha256:" + "a" * 64, description="d").gap_type
        'open_problem'
    """
    gap_id: str = Field(description="Unique id for this gap candidate.")
    gap_type: Literal["open_problem", "contradiction", "missing_link"] = Field(
        description="Kind of literature gap found."
    )
    context_ref: ArtifactRef = Field(
        description="Artifact ref of the retrieval transcript that grounds this gap."
    )
    description: str = Field(description="Concrete description grounded in retrieved evidence.")


# ====== 数值性/时间完整性审计（NoveltyEvidenceCollector，步骤 [5]） ======

class EvidenceRef(BaseModel):
    """A piece of prior work found to be near the hypothesis under review.

    Example:
        >>> EvidenceRef(ref_id="ev-1", kind="paper").kind
        'paper'
    """
    ref_id: str = Field(description="Identifier as reported by the retrieval tools (e.g. chunk id).")
    canonical_id: str = Field(default="", description="Canonical paper/dataset id if known.")
    kind: str = Field(description="Kind of work, e.g. paper, dataset, preprint.")
    version_status: str = Field(default="", description="preprint | published | retracted | unknown.")
    published_time: str = Field(default="", description="ISO date string if known, else empty.")
    content_ref: ArtifactRef | None = Field(
        default=None, description="Artifact ref to the full content, if separately persisted."
    )


class RetrievalCoverage(BaseModel):
    """Deterministic facts about one retrieval run, computed by code (not the LLM).

    Example:
        >>> RetrievalCoverage(channels_used=["paper_keyword_search"], retrieval_ceiling=5,
        ...                     decision_set_size=2, display_top_k=5, coverage_estimate=0.6,
        ...                     unrecalled_risk=0.2, index_version="sha256:" + "a" * 64,
        ...                     query_log_ref="sha256:" + "b" * 64).retrieval_ceiling
        5
    """
    channels_used: list[str] = Field(description="Tool names actually invoked during retrieval.")
    retrieval_ceiling: int = Field(ge=0, description="Configured max results considered per query.")
    decision_set_size: int = Field(ge=0, description="Number of nearest-work items retained.")
    display_top_k: int = Field(ge=0, description="Top-k configured for this run.")
    coverage_estimate: float = Field(ge=0, le=1, description="Estimated fraction of relevant literature covered.")
    unrecalled_risk: float = Field(ge=0, le=1, description="Estimated risk that relevant work was missed.")
    index_version: ArtifactRef = Field(
        description="Corpus artifact ref, doubling as a content-addressed version id."
    )
    query_log_ref: ArtifactRef = Field(description="Artifact ref of the full retrieval transcript.")


class TemporalIntegrity(BaseModel):
    """Time-related leakage/memorization risk assessment for one retrieval run.

    Example:
        >>> TemporalIntegrity(citation_cutoff_ok=True, retrieval_cutoff_ok=True,
        ...                     index_snapshot_time="2026-07-30T00:00:00+00:00",
        ...                     model_training_cutoff_known=None, post_cutoff_similarity=0.1,
        ...                     possible_memorization=False, leakage_risk=0.1,
        ...                     historical_backtest_validity=True).leakage_risk
        0.1
    """
    citation_cutoff_ok: bool = Field(description="Whether cited work respects the intended time cutoff.")
    retrieval_cutoff_ok: bool = Field(description="Whether retrieved work respects the intended time cutoff.")
    index_snapshot_time: str = Field(description="ISO timestamp when this retrieval ran.")
    model_training_cutoff_known: str | None = Field(
        default=None, description="Generator model's known training cutoff, if any."
    )
    post_cutoff_similarity: float = Field(
        ge=0, le=1, description="Similarity to work plausibly published after the model's training cutoff."
    )
    possible_memorization: bool = Field(description="Whether the hypothesis could be memorized rather than novel.")
    leakage_risk: float = Field(ge=0, le=1, description="Overall temporal leakage risk estimate.")
    historical_backtest_validity: bool = Field(
        description="Whether a historical backtest would be valid (no lookahead bias)."
    )


class NoveltyEvidenceJudgment(BaseModel):
    """LLM-authored subset of a NoveltyEvidenceReport; idea_id/refs are assigned by code.

    Example:
        >>> NoveltyEvidenceJudgment(nearest_work=[], facet_overlap={}, coverage_estimate=0.5,
        ...     unrecalled_risk=0.2, citation_cutoff_ok=True, retrieval_cutoff_ok=True,
        ...     post_cutoff_similarity=0.1, possible_memorization=False, leakage_risk=0.1,
        ...     historical_backtest_validity=True, uncertainty=0.2).uncertainty
        0.2
    """
    nearest_work: list[EvidenceRef] = Field(default_factory=list, description="Most similar prior work found.")
    facet_overlap: dict[str, float] = Field(
        default_factory=dict, description="Overlap score per facet in FACETS, each in [0, 1]."
    )
    coverage_estimate: float = Field(ge=0, le=1, description="Estimated fraction of relevant literature covered.")
    unrecalled_risk: float = Field(ge=0, le=1, description="Estimated risk that relevant work was missed.")
    citation_cutoff_ok: bool = Field(description="Whether cited work respects the intended time cutoff.")
    retrieval_cutoff_ok: bool = Field(description="Whether retrieved work respects the intended time cutoff.")
    post_cutoff_similarity: float = Field(ge=0, le=1, description="Similarity to plausibly post-cutoff work.")
    possible_memorization: bool = Field(description="Whether the hypothesis could be memorized rather than novel.")
    leakage_risk: float = Field(ge=0, le=1, description="Overall temporal leakage risk estimate.")
    historical_backtest_validity: bool = Field(description="Whether a historical backtest would be valid.")
    uncertainty: float = Field(ge=0, le=1, description="Overall uncertainty in this novelty assessment.")

    @model_validator(mode="after")
    def check_facet_keys(self) -> "NoveltyEvidenceJudgment":
        # facet_overlap 的键必须是 FACETS 常量的子集,否则 hard_gate 里按 facet 均值算重叠度时
        # 会悄悄把未知维度也算进去
        unknown = set(self.facet_overlap) - set(FACETS)
        if unknown:
            raise ValueError(f"facet_overlap has unknown facets: {sorted(unknown)}")
        return self


class NoveltyEvidenceReport(BaseModel):
    """Numerical novelty/temporal-integrity audit result; no verdict field (hard_gate judges).

    Example:
        >>> NoveltyEvidenceReport(idea_id="idea-1", nearest_work=[], facet_overlap={},
        ...     coverage_ref="sha256:" + "a" * 64, temporal_ref="sha256:" + "b" * 64,
        ...     uncertainty=0.2).idea_id
        'idea-1'
    """
    idea_id: str = Field(description="Candidate id.")
    nearest_work: list[EvidenceRef] = Field(default_factory=list, description="Most similar prior work found.")
    facet_overlap: dict[str, float] = Field(
        default_factory=dict, description="Overlap score per facet in FACETS, each in [0, 1]."
    )
    coverage_ref: ArtifactRef = Field(description="Artifact ref of the RetrievalCoverage record.")
    temporal_ref: ArtifactRef = Field(description="Artifact ref of the TemporalIntegrity record.")
    query_log_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact ref of the raw retrieval transcript, reused by the "
                    "domain_consistency reviewer as starting context. None when novelty "
                    "retrieval failed and this is a degraded empty report.",
    )
    input_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact ref of the input prompt this report was produced from; the "
                    "staleness fingerprint. None means the report came from a failure "
                    "fallback and must always be treated as stale.",
    )
    uncertainty: float = Field(ge=0, le=1, description="Overall uncertainty in this novelty assessment.")


# ====== 验证方案（ValidationPlanner + VerifierRegistry，步骤 [7]） ======

class VerifierSpec(BaseModel):
    """A concrete, matched verification procedure.

    Example:
        >>> VerifierSpec(verifier_type="ablation_replication", applicable_domains=["ai4s"],
        ...     observable_vars=["metric"], statistical_assumptions=["same seed budget"],
        ...     success_condition="s", failure_condition="f", inconclusive_condition="i",
        ...     cost_ref="sha256:" + "a" * 64, supports_auto_exec=True,
        ...     requires_human_approval=False).verifier_type
        'ablation_replication'
    """
    verifier_type: str = Field(description="Verifier identifier.")
    applicable_domains: list[str] = Field(description="Domains this verifier applies to.")
    observable_vars: list[str] = Field(description="Variables this verifier requires to be observable.")
    statistical_assumptions: list[str] = Field(description="Assumptions the verifier relies on.")
    success_condition: str = Field(description="Condition under which the verifier reports success.")
    failure_condition: str = Field(description="Condition under which the verifier reports failure.")
    inconclusive_condition: str = Field(description="Condition under which the verifier is inconclusive.")
    cost_ref: ArtifactRef = Field(description="Artifact ref of the estimated cost for this verifier run.")
    supports_auto_exec: bool = Field(description="Whether this verifier can run without human execution.")
    requires_human_approval: bool = Field(description="Whether a human must approve before running it.")


class ValidationPlan(BaseModel):
    """Validation plan for one candidate; verifier=None triggers EXPLORATORY in hard_gate.

    Example:
        >>> ValidationPlan(idea_id="idea-1", minimal_test="t", verifier=None,
        ...     decision_rule="EXPLORATORY",
        ...     estimated_cost_ref="sha256:" + "a" * 64).verifier is None
        True
    """
    idea_id: str = Field(description="Candidate id.")
    minimal_test: str = Field(description="Smallest test that would exercise this hypothesis.")
    verifier: VerifierSpec | None = Field(
        default=None, description="Matched verifier; None means no verifier was available (EXPLORATORY)."
    )
    decision_rule: str = Field(description="Explicit PASS/FAIL/INCONCLUSIVE rule text.")
    estimated_cost_ref: ArtifactRef = Field(description="Artifact ref of the estimated cost for this plan.")


# ====== 反方审阅（SkepticReviewer，步骤 [6]） ======

class SkepticJudgment(BaseModel):
    """LLM-authored subset of a SkepticReport; idea_id is assigned by code.

    Example:
        >>> SkepticJudgment(critique="c", unaddressed_risks=[], fatal_flaw_found=False).fatal_flaw_found
        False
    """
    critique: str = Field(
        description="Independent critique, written without access to the generator's "
                    "self-assessed confidence score."
    )
    unaddressed_risks: list[str] = Field(default_factory=list, description="Risks the package does not address.")
    fatal_flaw_found: bool = Field(
        description="Whether an unfixable flaw was found, as opposed to a risk revision could still address."
    )


class SkepticReport(BaseModel):
    """Design-time skeptic review result; not itself a verdict (hard_gate judges).

    Example:
        >>> SkepticReport(idea_id="idea-1", perspective="methodology", critique="c",
        ...                 unaddressed_risks=[], fatal_flaw_found=False).idea_id
        'idea-1'
    """
    idea_id: str = Field(description="Candidate id.")
    perspective: str = Field(
        description="Review perspective id this report came from; matches "
                    "ReviewPerspective.perspective_id and the rubric item suffix risk_ok_*."
    )
    critique: str = Field(description="Independent critique text.")
    unaddressed_risks: list[str] = Field(default_factory=list, description="Risks the package does not address.")
    fatal_flaw_found: bool = Field(description="Whether an unfixable flaw was found.")
    transcript_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact ref of the retrieval transcript; only perspectives with "
                    "tools produce one.",
    )
    input_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact ref of the input prompt this report was produced from; the "
                    "staleness fingerprint. None means the report came from a failure "
                    "fallback and must always be treated as stale.",
    )
    failed: bool = Field(
        default=False,
        description="True when this perspective's call failed. hard_gate treats a failed "
                    "review as not passing (fail-closed): a review that did not run must "
                    "never count as a review that approved.",
    )


# ====== Verbalized Sampling（多候选生成，步骤 [3]） ======

class VerbalizedSamplingResponse(BaseModel):
    """LLM output wrapper for one Verbalized Sampling call: several self-scored candidates at once.

    Example:
        >>> VerbalizedSamplingResponse(candidates=[]).candidates
        []
    """
    candidates: list[HypothesisDraft] = Field(
        description="Multiple falsifiable hypothesis drafts, each with its own sampling_probability."
    )


# ====== PairwiseJudge（HypoPriList 排序用比较器，步骤 [9]） ======

class PairwiseJudgment(BaseModel):
    """LLM output for one anonymized pairwise comparison.

    Example:
        >>> PairwiseJudgment(winner="candidate_a", rationale="more falsifiable").winner
        'candidate_a'
    """
    winner: Literal["candidate_a", "candidate_b"] = Field(description="Which anonymized candidate won.")
    rationale: str = Field(description="Itemized justification, not a bare preference.")


# ====== 修订闭环（RevisionLoop，步骤 [8] 之后） ======

class RevisionDraft(BaseModel):
    """LLM-authored revision of one blocked candidate.

    Deliberately carries no sampling_probability field: the reviser writes the rebuttal that
    is shown to the blocking reviewer, so letting it see the generator's self-assessed
    confidence would leak that score into the review side through the rebuttal text
    (Co-Scientist: reviewers must not anchor on the generator's self-assessment).

    Example:
        >>> RevisionDraft(rebuttal="control arm added", changes_made=["added control"],
        ...     revised_novel_hypothesis="X causes Y", revised_premises=[],
        ...     revised_predicted_observations=["p"],
        ...     revised_disconfirming_observations=["d"]).rebuttal
        'control arm added'
    """
    rebuttal: str = Field(
        description="Reply to the blocking reviewer's critique; shown to that reviewer."
    )
    changes_made: list[str] = Field(
        default_factory=list,
        description="One entry per change: what was changed and which risk it addresses.",
    )
    revised_novel_hypothesis: str = Field(description="The revised novel claim under test.")
    revised_premises: list[ClaimEvidence] = Field(description="Revised evidence-bound premises.")
    revised_predicted_observations: list[str] = Field(
        description="Revised observations predicted if the hypothesis is true."
    )
    revised_disconfirming_observations: list[str] = Field(
        description="Revised observations that would refute the hypothesis."
    )

    @model_validator(mode="after")
    def check_revision_stays_falsifiable(self) -> "RevisionDraft":
        # 与 HypothesisPackage 相同的不变量。放在 LLM 面向的 schema 上，让 pydantic-ai 的输出
        # 校验重试原生接管"返回了但内容不合法"这一类，无需我们写重试代码。
        if not self.revised_predicted_observations or not self.revised_disconfirming_observations:
            raise ValueError("revision requires predictions and disconfirmers")
        return self


class RevisionRound(BaseModel):
    """Audit record for one debate round. Not a verdict.

    Example:
        >>> RevisionRound(round_index=1, debated_perspective="methodology",
        ...     package_ref="sha256:" + "a" * 64, rebuttal_ref="sha256:" + "b" * 64,
        ...     cleared=False).round_index
        1
    """
    round_index: int = Field(ge=1, description="1-based debate round number.")
    debated_perspective: str = Field(
        description="Perspective id the reviser debated against; fixed for the whole loop."
    )
    package_ref: ArtifactRef = Field(description="Artifact ref of this round's revised package.")
    rebuttal_ref: ArtifactRef = Field(description="Artifact ref of this round's rebuttal text.")
    reviewer_response_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact ref of the opponent's response; None when that call failed.",
    )
    cleared: bool = Field(
        description="Whether the blocked rubric item was cleared after this round. This is a "
                    "local predicate on the debated item only - it does NOT predict the final "
                    "verdict, because the closing refresh re-runs the other perspectives and "
                    "the final gate may block on a different item.",
    )


# ====== 全流程审计轨迹 ======

class PipelineCandidateResult(BaseModel):
    """Full audit trail for one candidate through run_full_pipeline.

    novelty/validation_plan stay None, and reviews stays empty, when pre_gate already REVISEd
    the candidate, since the expensive [5]-[7] steps are skipped for candidates that fail the
    cheap [4] check first.

    Example:
        >>> PipelineCandidateResult(package=package, structural=structural,
        ...     falsifiability=falsifiability, decision=decision).novelty  # doctest: +SKIP
    """
    package: HypothesisPackage
    structural: StructuralCheckReport
    falsifiability: FalsifiabilityReport
    novelty: NoveltyEvidenceReport | None = None
    reviews: list[SkepticReport] = Field(
        default_factory=list,
        description="One report per review perspective. Empty list means the candidate was "
                    "REVISEd at pre_gate and never reached the review stage - distinct from "
                    "novelty/validation_plan which use None for the same situation.",
    )
    validation_plan: ValidationPlan | None = None
    decision: GateDecision
    revisions: list[RevisionRound] = Field(
        default_factory=list,
        description="One entry per debate round; empty means the candidate never entered "
                    "the revision loop.",
    )
    revision_blocking_factor: str | None = Field(
        default=None,
        description="The rubric item that sent this candidate into the revision loop. Fixed "
                    "for the whole loop, hence recorded here rather than per RevisionRound.",
    )
