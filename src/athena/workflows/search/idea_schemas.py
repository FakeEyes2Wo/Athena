"""Idea Generation P0（pre_gate 闭环）专用的结构化事实模型。

这些模型只服务本模块，不进入 core/schemas.py 的跨平面共享模型集合；所有字段的 description
与后续 Prompt 均使用英文；任何可行性/新颖性/可证伪性判断只在 gatekeeper.pre_gate 中产出
verdict，本文件的报告类模型均不带 verdict 字段。
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ====== 常量 ======

GATE_RUBRIC_VERSION: str = "gate-rubric/v1"


# ====== 枚举 ======

class ClaimRole(str, Enum):
    """Layered role of a claim inside a hypothesis package."""
    SUPPORTED_PREMISE = "supported_premise"
    DERIVED_INFERENCE = "derived_inference"
    NOVEL_HYPOTHESIS = "novel_hypothesis"
    PREDICTION = "prediction"
    DISCONFIRMER = "disconfirming_observation"


class GateVerdict(str, Enum):
    """Gate verdict values. P0 pre_gate only ever produces PASS or REVISE."""
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
    """Full structured hypothesis package. P0 keeps it in memory; no ArtifactStore persistence yet.

    Example:
        >>> HypothesisPackage(idea_id="idea-1", generation_strategy="single_strategy_v1",
        ...                    novel_hypothesis="n", supported_premises=[], inference_chain=[],
        ...                    predicted_observations=["p"], disconfirming_observations=["d"],
        ...                    lineage_op="generate").idea_id
        'idea-1'
    """
    idea_id: str = Field(description="Same id as the Hypothesis node.")
    generation_strategy: str = Field(description="Strategy that produced this candidate.")
    novel_hypothesis: str = Field(description="The novel claim under test.")
    supported_premises: list[ClaimEvidence] = Field(description="Evidence-bound premises.")
    inference_chain: list[InferenceStep] = Field(description="Explicit reasoning steps.")
    predicted_observations: list[str] = Field(description="Observations predicted if true.")
    disconfirming_observations: list[str] = Field(description="Observations that would refute it.")
    validation_plan_ref: str | None = Field(
        default=None,
        description="ValidationPlan artifact ref; stays None until validation planning (out of P0 scope).",
    )
    lineage_op: str = Field(description="generate | specialize | merge | mutate | branch.")

    @model_validator(mode="after")
    def check_novel_hypothesis_testability(self) -> "HypothesisPackage":
        # 与 HypothesisDraft 相同的不变量；在最终落盘对象上再校验一次，保证审计对象自身自洽
        if not self.predicted_observations or not self.disconfirming_observations:
            raise ValueError("novel hypothesis requires predictions and disconfirmers")
        return self


class ResearchProblemInput(BaseModel):
    """P0 简化版问题定格输入：不接 ArtifactStore，证据直接以文本列表传入。

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
    """Decision produced by pre_gate; the only place a verdict is produced in this module.

    Example:
        >>> GateDecision(idea_id="idea-1", gate_phase="pre_gate", verdict=GateVerdict.PASS,
        ...                rubric_version=GATE_RUBRIC_VERSION, item_scores=[]).verdict
        <GateVerdict.PASS: 'PASS'>
    """
    idea_id: str = Field(description="Candidate id.")
    gate_phase: Literal["pre_gate", "full"] = Field(
        description="P0 only produces pre_gate; full hard_gate is out of scope."
    )
    verdict: GateVerdict = Field(description="Gate verdict.")
    rubric_version: str = Field(description="Versioned rubric used, e.g. gate-rubric/v1.")
    item_scores: list[RubricItemScore] = Field(
        description="Per-item scores with evidence: pre_gate has 2 items "
                    "(evidence_traceable, falsifiable)."
    )
    blocking_factor: str | None = Field(default=None, description="First blocking item if any.")
