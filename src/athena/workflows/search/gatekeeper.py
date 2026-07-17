"""pre_gate：唯一产出 GateVerdict 的地方（步骤 [4]）。

P0 简化规则：结构或可证伪性任一不满足 -> REVISE；"核心主张不可证伪且无法修正 -> REJECT"这个
需要额外可修正性判断的分支不在 P0 范围内，留给后续 hard_gate 迭代。
"""

from athena.workflows.search.idea_schemas import (
    GATE_RUBRIC_VERSION,
    FalsifiabilityReport,
    GateDecision,
    GateVerdict,
    RubricItemScore,
    StructuralCheckReport,
)


def pre_gate(structural: StructuralCheckReport, falsifiability: FalsifiabilityReport) -> GateDecision:
    """依据 StructuralCheckReport 与 FalsifiabilityReport 产出 pre_gate 阶段的 GateDecision。

    Example:
        >>> pre_gate(structural_report, falsifiability_report).verdict  # doctest: +SKIP
        <GateVerdict.PASS: 'PASS'>
    """
    if structural.idea_id != falsifiability.idea_id:
        raise ValueError("structural and falsifiability reports refer to different ideas")

    evidence_traceable_ok = structural.premise_evidence_ok and structural.novel_hypothesis_testable
    evidence_traceable_score = RubricItemScore(
        item="evidence_traceable",
        score=1.0 if evidence_traceable_ok else 0.0,
        evidence=(
            "all premises cite evidence and predictions/disconfirmers are present"
            if evidence_traceable_ok
            else f"structural violations: {structural.violations}"
        ),
    )
    falsifiable_score = RubricItemScore(
        item="falsifiable",
        score=1.0 if falsifiability.is_falsifiable else 0.0,
        evidence=(
            falsifiability.testable_implication
            if falsifiability.is_falsifiable
            else f"unobservable variables: {falsifiability.unobservable_variables}"
        ),
    )

    if evidence_traceable_ok and falsifiability.is_falsifiable:
        verdict = GateVerdict.PASS
        blocking_factor = None
    elif not evidence_traceable_ok:
        verdict = GateVerdict.REVISE
        blocking_factor = "evidence_traceable"
    else:
        verdict = GateVerdict.REVISE
        blocking_factor = "falsifiable"

    return GateDecision(
        idea_id=structural.idea_id,
        gate_phase="pre_gate",
        verdict=verdict,
        rubric_version=GATE_RUBRIC_VERSION,
        item_scores=[evidence_traceable_score, falsifiable_score],
        blocking_factor=blocking_factor,
    )
