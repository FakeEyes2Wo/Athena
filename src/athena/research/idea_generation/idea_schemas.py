"""Structured facts for Idea Generation screening and independent review.

这些模型只服务本模块，不进入 core/schemas.py 的跨平面共享模型集合；所有字段的 description
与后续 Prompt 均使用英文。

一条贯穿本文件的约定：任何可行性/可证伪性判断的 **verdict 只在 gatekeeper 里产出**
（pre_gate 或 light_hard_gate），所以除 GateDecision 外，本文件所有报告类模型都不带
verdict 字段，只承载可审计的事实与逐项打分。另一条约定：凡是 ``XxxJudgment`` 都是
"由 LLM 撰写的子集"，对应的 ``XxxReport`` 才是最终对象，其中 idea_id / 各种 ArtifactRef
一律由代码分配，不向 LLM 索要，避免幻觉 id。
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from athena.core.contracts import ArtifactRef

# 常量

GATE_RUBRIC_VERSION: str = "gate-rubric/v2"


# 枚举


class ClaimRole(str, Enum):
    """Layered role of a claim inside a hypothesis package."""

    SUPPORTED_PREMISE = "supported_premise"
    DERIVED_INFERENCE = "derived_inference"
    NOVEL_HYPOTHESIS = "novel_hypothesis"
    PREDICTION = "prediction"
    DISCONFIRMER = "disconfirming_observation"


class GateVerdict(str, Enum):
    """Gate verdict values. pre_gate only ever produces PASS or REVISE; light_hard_gate
    can also produce REJECT when revision cannot fix a flaw."""

    PASS = "PASS"
    REVISE = "REVISE"
    REJECT = "REJECT"


# 数据模型


class ClaimEvidence(BaseModel):
    """One claim plus its layered role; supported premises must cite evidence.

    Example:
        >>> ClaimEvidence(claim="X causes Y", role=ClaimRole.SUPPORTED_PREMISE,
        ...                supporting_refs=["ev-0"]).role
        <ClaimRole.SUPPORTED_PREMISE: 'supported_premise'>
    """

    claim: str = Field(description="Claim text.")
    role: ClaimRole = Field(description="Layered role of this claim.")
    supporting_refs: list[str] = Field(
        default_factory=list, description="Evidence ref ids."
    )

    @model_validator(mode="after")
    def check_layered_rule(self) -> "ClaimEvidence":
        """Enforce evidence binding for supported and novel claim roles."""
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
    uncertainty: float = Field(
        ge=0, le=1, description="Subjective uncertainty in [0,1]."
    )


class IdeatorHypothesisDraft(BaseModel):
    """LLM-authored subset for the live EDA-grounded Ideator (agents/ideator_agent.py).

    idea_id / generation_strategy / lineage_op are assigned by code, not requested from
    the LLM, so they can't be hallucinated or collide.

    Example:
        >>> IdeatorHypothesisDraft(statement="s", intervention="i", expected_effect="e",
        ...     supported_premises=[], predicted_observations=["p"],
        ...     disconfirming_observations=["d"]).statement
        's'
    """

    statement: str = Field(description="Falsifiable hypothesis statement (short).")
    intervention: str = Field(description="Minimal change or observation to test it.")
    expected_effect: str = Field(description="Expected measurable effect.")
    supported_premises: list[ClaimEvidence] = Field(
        description="Evidence-bound premises."
    )
    inference_chain: list[InferenceStep] = Field(
        default_factory=list, description="Explicit reasoning steps."
    )
    predicted_observations: list[str] = Field(
        description="Observations predicted if true."
    )
    disconfirming_observations: list[str] = Field(
        description="Observations that would refute it."
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Keys of papers actually read from the literature corpus while forming this "
        "hypothesis. Leave empty when no corpus was available; never invent keys.",
    )

    @model_validator(mode="after")
    def check_novel_hypothesis_testability(self) -> "IdeatorHypothesisDraft":
        """Require predictions and disconfirmers for a testable hypothesis."""
        if not self.predicted_observations or not self.disconfirming_observations:
            raise ValueError("novel hypothesis requires predictions and disconfirmers")
        return self


class IdeatorHypothesisBatch(BaseModel):
    """Structured output contract for the live EDA-grounded Ideator agent.

    Example:
        >>> IdeatorHypothesisBatch(hypotheses=[]).hypotheses
        []
    """

    hypotheses: list[IdeatorHypothesisDraft] = Field(
        min_length=1, max_length=5, description="1-5 falsifiable hypotheses."
    )
    eda_request: str | None = Field(
        default=None,
        description="Optional additional-EDA request; the runtime dispatches a Data "
        "Agent to satisfy it before the next Ideator round.",
    )


class FalsifiabilityJudgment(BaseModel):
    """LLM-authored subset of a falsifiability report; idea_id is assigned by code.

    Example:
        >>> FalsifiabilityJudgment(testable_implication="t", unobservable_variables=[],
        ...                         is_falsifiable=True).is_falsifiable
        True
    """

    testable_implication: str = Field(
        default="", description="The testable implication found, if any."
    )
    unobservable_variables: list[str] = Field(
        default_factory=list, description="Variables that block observability."
    )
    is_falsifiable: bool = Field(
        description="Whether a testable implication was designed."
    )

    @model_validator(mode="after")
    def check_falsifiable_has_evidence(self) -> "FalsifiabilityJudgment":
        """Require evidence text whenever the judgment is falsifiable."""
        # gatekeeper 会把 testable_implication 原文当作 falsifiable 这一项的 evidence；
        # PASS 不允许带空证据，故在此收紧不变量。
        if self.is_falsifiable and not self.testable_implication.strip():
            raise ValueError(
                "falsifiable judgment requires a non-empty testable_implication"
            )
        return self


class FalsifiabilityReport(BaseModel):
    """Design-time falsifiability check result; not a verdict.

    Example:
        >>> FalsifiabilityReport(idea_id="idea-1", testable_implication="t",
        ...                        unobservable_variables=[], is_falsifiable=True).idea_id
        'idea-1'
    """

    idea_id: str = Field(description="Candidate id.")
    testable_implication: str = Field(
        default="", description="The testable implication found, if any."
    )
    unobservable_variables: list[str] = Field(
        default_factory=list, description="Variables that block observability."
    )
    is_falsifiable: bool = Field(
        description="Whether a testable implication was designed."
    )

    @model_validator(mode="after")
    def check_falsifiable_has_evidence(self) -> "FalsifiabilityReport":
        """Require evidence text on a falsifiable persisted report."""
        # 与 FalsifiabilityJudgment 相同的不变量；在最终落盘对象上再校验一次
        if self.is_falsifiable and not self.testable_implication.strip():
            raise ValueError(
                "falsifiable report requires a non-empty testable_implication"
            )
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
    evidence_refs: list[str] = Field(
        default_factory=list, description="Evidence ref ids."
    )


class GateDecision(BaseModel):
    """Decision produced by pre_gate or light_hard_gate; the only model carrying a verdict.

    Example:
        >>> GateDecision(idea_id="idea-1", gate_phase="pre_gate", verdict=GateVerdict.PASS,
        ...                rubric_version=GATE_RUBRIC_VERSION, item_scores=[]).verdict
        <GateVerdict.PASS: 'PASS'>
    """

    idea_id: str = Field(description="Candidate id.")
    gate_phase: Literal["pre_gate", "full"] = Field(
        description="pre_gate produces this in phase 'pre_gate'; light_hard_gate produces it in phase 'full'."
    )
    verdict: GateVerdict = Field(description="Gate verdict.")
    rubric_version: str = Field(
        description="Versioned rubric used, e.g. gate-rubric/v2."
    )
    item_scores: list[RubricItemScore] = Field(
        description="Per-item scores with evidence: pre_gate always has 2 items "
        "(evidence_traceable, falsifiable); light_hard_gate adds risk_total and one "
        "risk_ok_<perspective> per review perspective."
    )
    blocking_factor: str | None = Field(
        default=None, description="First blocking item if any."
    )


# 反方审阅（SkepticReviewer）


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
    unaddressed_risks: list[str] = Field(
        default_factory=list, description="Risks the package does not address."
    )
    fatal_flaw_found: bool = Field(
        description="Whether an unfixable flaw was found, as opposed to a risk revision could still address."
    )


class SkepticReport(BaseModel):
    """Design-time skeptic review result; not itself a verdict (light_hard_gate judges).

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
    unaddressed_risks: list[str] = Field(
        default_factory=list, description="Risks the package does not address."
    )
    fatal_flaw_found: bool = Field(description="Whether an unfixable flaw was found.")
    input_ref: ArtifactRef | None = Field(
        default=None,
        description="Artifact ref of the input prompt this report was produced from; the "
        "staleness fingerprint. None means the report came from a failure "
        "fallback and must always be treated as stale.",
    )
    failed: bool = Field(
        default=False,
        description="True when this perspective's call failed. light_hard_gate treats a failed "
        "review as not passing (fail-closed): a review that did not run must "
        "never count as a review that approved.",
    )
