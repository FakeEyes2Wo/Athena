"""gatekeeper：整条 Idea Generation 流水线里唯一产出 GateVerdict 的地方。

两个门：
- pre_gate（步骤 [4]，2 项 rubric）：只看结构完整性与可证伪性，任一不满足 -> REVISE。它是
  廉价前置筛子，跑在昂贵的检索/审阅步骤之前，不合格的候选直接淘汰。
- hard_gate（步骤 [8]，5 项 rubric）：在 pre_gate 两项之上追加 novelty_ok/verifier_ok/
  risk_ok，产出 gate_phase="full" 的终审判决。

设计参考：verdict 判定本质是准入谓词（对应 AutoSOTA 论文里的 Adm(h)——逐项校验是否保持
评估完整性，不满足则拒绝/退回修改，而不是给一个模糊的总分）；itemized rubric + 强制 REVISE
（而不是静默放行）也呼应 Co-Scientist 论文里"审阅独立于生成、不自我批准"的设计
（AI co-scientist, arXiv:2502.18864）。
"""

from athena.workflows.search.idea_schemas import (
    GATE_RUBRIC_VERSION,
    FalsifiabilityReport,
    GateDecision,
    GateVerdict,
    NoveltyEvidenceReport,
    RubricItemScore,
    SkepticReport,
    StructuralCheckReport,
    ValidationPlan,
)


# ====== 常量 ======

NOVELTY_OVERLAP_THRESHOLD: float = 0.7
"""facet_overlap 均值达到或超过此阈值,判定为与已有工作重叠过高(不够新颖)。"""

MAX_TOLERATED_RISKS: int = 2
"""hard_gate 容忍的"未处理风险"条数上限；反方审阅几乎总能挑出一两条风险,若一条就拦下来,
没有候选能走到步骤 [9] 排序。与 NOVELTY_OVERLAP_THRESHOLD 同理,用阈值而非布尔量判定。"""


# ====== 两个门共用的 rubric 项 ======

def structural_rubric_scores(
    structural: StructuralCheckReport, falsifiability: FalsifiabilityReport
) -> tuple[bool, RubricItemScore, RubricItemScore]:
    """产出 pre_gate 与 hard_gate 共用的前两项 rubric（evidence_traceable / falsifiable），
    外加 evidence_traceable 这个派生判定本身（它不是报告上的现成字段，是两个布尔的合取）。

    两个门必须对同一份报告给出完全一致的评分与证据文案，写两遍会在后续改动时悄悄分叉，
    因此收敛到一处。

    Example:
        >>> ok, traceable, falsifiable = structural_rubric_scores(structural, falsifiability)  # doctest: +SKIP
        >>> traceable.item
        'evidence_traceable'
    """
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
    return evidence_traceable_ok, evidence_traceable_score, falsifiable_score


# ====== 门控逻辑 ======

def pre_gate(structural: StructuralCheckReport, falsifiability: FalsifiabilityReport) -> GateDecision:
    """依据 StructuralCheckReport 与 FalsifiabilityReport 产出 pre_gate 阶段的 GateDecision。

    Example:
        >>> pre_gate(structural_report, falsifiability_report).verdict  # doctest: +SKIP
        <GateVerdict.PASS: 'PASS'>
    """
    if structural.idea_id != falsifiability.idea_id:
        raise ValueError("structural and falsifiability reports refer to different ideas")

    evidence_traceable_ok, evidence_traceable_score, falsifiable_score = structural_rubric_scores(
        structural, falsifiability
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


def hard_gate(
    structural: StructuralCheckReport,
    falsifiability: FalsifiabilityReport,
    novelty: NoveltyEvidenceReport,
    skeptic: SkepticReport,
    validation_plan: ValidationPlan,
) -> GateDecision:
    """依据 5 份报告产出 hard_gate 阶段的 GateDecision(gate_phase="full")。

    判定优先级（从上到下短路，blocking_factor 记第一个拦住它的项）：
    1. 结构/可证伪性问题 -> REVISE（设计上可修正，不管别的项是否也失败）
    2. facet_overlap 为空（新颖性缺证据） -> REVISE（补一轮检索即可修正）
    3. facet_overlap 均值超阈值（确实与已有工作重叠） -> REJECT（不是靠修订能解决的）
    4. 反方审阅发现致命缺陷 -> REJECT
    5. 无致命缺陷但"未处理风险"条数超过 MAX_TOLERATED_RISKS -> REVISE
    6. 前四项都过、只是没有可用 verifier -> EXPLORATORY 而不是拒绝，呼应设计文档
       "无可用 verifier 就标 EXPLORATORY"的规则

    uncertainty 只写进 novelty_ok 这一项的 evidence 供人工审计，不参与判定——它的阈值缺少
    实验依据，等 Kaggle 对比实验定完 constraint_strictness 档位后再决定要不要门控。

    Example:
        >>> hard_gate(structural, falsifiability, novelty, skeptic, plan).gate_phase  # doctest: +SKIP
        'full'
    """
    idea_ids = {structural.idea_id, falsifiability.idea_id, novelty.idea_id, skeptic.idea_id,
                validation_plan.idea_id}
    if len(idea_ids) != 1:
        raise ValueError(f"reports refer to different ideas: {sorted(idea_ids)}")
    idea_id = structural.idea_id

    evidence_traceable_ok, evidence_traceable_score, falsifiable_score = structural_rubric_scores(
        structural, falsifiability
    )

    # facet_overlap 为空 = 检索侧一项重叠度都没打出来，此时"新颖"是没有证据支撑的默认值，
    # 不能当成通过——否则均值 0.0 会让"什么都没查到"自动拿满分，与"任何判断必须绑定逐项
    # 证据"的硬约束相反。这种情况判 REVISE（补检索可修正），而不是 REJECT。
    novelty_evidence_missing = not novelty.facet_overlap
    mean_overlap = (
        sum(novelty.facet_overlap.values()) / len(novelty.facet_overlap)
        if novelty.facet_overlap else 0.0
    )
    novelty_ok = not novelty_evidence_missing and mean_overlap < NOVELTY_OVERLAP_THRESHOLD
    novelty_score = RubricItemScore(
        item="novelty_ok", score=1.0 if novelty_ok else 0.0,
        evidence=(
            f"no facet_overlap scores were produced, so novelty is unsupported by evidence "
            f"(uncertainty={novelty.uncertainty:.2f})"
            if novelty_evidence_missing
            else f"mean facet_overlap={mean_overlap:.2f} (threshold {NOVELTY_OVERLAP_THRESHOLD}), "
                 f"uncertainty={novelty.uncertainty:.2f}; "
                 f"nearest_work={[w.ref_id for w in novelty.nearest_work]}"
        ),
    )

    verifier_ok = validation_plan.verifier is not None
    verifier_score = RubricItemScore(
        item="verifier_ok", score=1.0 if verifier_ok else 0.0,
        evidence=(
            f"verifier={validation_plan.verifier.verifier_type}" if verifier_ok
            else "no verifier matched; validation plan is EXPLORATORY"
        ),
    )

    risk_ok = not skeptic.fatal_flaw_found and len(skeptic.unaddressed_risks) <= MAX_TOLERATED_RISKS
    risk_score = RubricItemScore(
        item="risk_ok", score=1.0 if risk_ok else 0.0,
        evidence=f"unaddressed_risks={len(skeptic.unaddressed_risks)} (tolerance {MAX_TOLERATED_RISKS}): "
                 f"{skeptic.unaddressed_risks}; fatal_flaw_found={skeptic.fatal_flaw_found}",
    )

    item_scores = [evidence_traceable_score, falsifiable_score, novelty_score, verifier_score, risk_score]

    if not evidence_traceable_ok or not falsifiability.is_falsifiable:
        verdict = GateVerdict.REVISE
        blocking_factor = "evidence_traceable" if not evidence_traceable_ok else "falsifiable"
    elif novelty_evidence_missing:
        verdict = GateVerdict.REVISE
        blocking_factor = "novelty_ok"
    elif not novelty_ok:
        verdict = GateVerdict.REJECT
        blocking_factor = "novelty_ok"
    elif skeptic.fatal_flaw_found:
        verdict = GateVerdict.REJECT
        blocking_factor = "risk_ok"
    elif not risk_ok:
        verdict = GateVerdict.REVISE
        blocking_factor = "risk_ok"
    elif not verifier_ok:
        verdict = GateVerdict.EXPLORATORY
        blocking_factor = "verifier_ok"
    else:
        verdict = GateVerdict.PASS
        blocking_factor = None

    return GateDecision(
        idea_id=idea_id, gate_phase="full", verdict=verdict, rubric_version=GATE_RUBRIC_VERSION,
        item_scores=item_scores, blocking_factor=blocking_factor,
    )
