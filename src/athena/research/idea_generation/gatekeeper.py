"""gatekeeper：整条 Idea Generation light pipeline 里唯一产出 GateVerdict 的地方。

两个门：
- pre_gate（2 项 rubric）：只看结构完整性与可证伪性，任一不满足 -> REVISE。它是廉价前置
  筛子，跑在昂贵的审阅步骤之前，不合格的候选直接淘汰。
- light_hard_gate（4 + 视角数 项 rubric）：在 pre_gate 两项之上追加 verifier_ok /
  risk_total，外加每个审阅视角一项 risk_ok_<perspective>，产出 gate_phase="full" 的终审
  判决。

设计参考：verdict 判定本质是准入谓词（对应 AutoSOTA 论文里的 Adm(h)——逐项校验是否保持
评估完整性，不满足则拒绝/退回修改，而不是给一个模糊的总分）；itemized rubric + 强制 REVISE
（而不是静默放行）也呼应 Co-Scientist 论文里"审阅独立于生成、不自我批准"的设计
（AI co-scientist, arXiv:2502.18864）。
"""

from athena.research.idea_generation.idea_schemas import (
    GATE_RUBRIC_VERSION,
    FalsifiabilityReport,
    GateDecision,
    GateVerdict,
    RubricItemScore,
    SkepticReport,
    StructuralCheckReport,
    ValidationPlan,
)

MAX_TOLERATED_RISKS: int = 6
"""单个视角"未处理风险"条数上限。

**这个值是实测校准过的，不是拍脑袋。** 初版取 2，依据是"反方审阅几乎总能挑出一两条
风险"这个假设；2026-08-15 在 MazeCrawler 上真实跑过 4 个候选后，实测每份审阅稳定产出
**4-5 条**风险（观测值 4,4,5,5,5,5,5,5，从未 ≤2），而同期 ``fatal_flaw_found`` 全为
False——审阅者并不认为这些假设有致命缺陷。也就是说 2 不是"严"，是数学上无人可过：
连续 3 轮重新提案全部卡在 ``risk_ok_*``，SEARCH 一个实验都跑不成。

改判逻辑因此转为**以审阅者自己的裁定（fatal_flaw_found）为主**：致命缺陷直接 REJECT，
而条数只作"明显失控"的兵线。按实测分布（4-5）取 6，即明显高于常规水平才拦。"""


def max_total_risks(perspective_count: int) -> int:
    """跨视角未处理风险总量上限，堵住"把风险摊到多个视角上规避门控"这条回归。

    **必须严格小于 MAX_TOLERATED_RISKS × 视角数，否则这一项是死代码。** 判定链里
    "任一视角超单项阈值"先于"总量超标"，所以后者要触发就必须"所有视角都不超单项阈值、
    但总量超标"；若本值 >= 单项阈值 × N，这两个条件数学上无法同时成立（所有视角 <= T
    蕴含总量 <= T×N），risk_total 就成了恒满分、永不作为 blocking_factor 的摆设。

    因此取 ``单项上限 × 视角数 - 1``（恰好满足不等式的最大值）：双视角 11、三视角 17。
    实测双视角总量为 9-10，落在放行区间内；只有当每个视角都逼近单项上限时才触发。

    Example:
        >>> max_total_risks(2)
        11
    """
    if perspective_count < 1:
        raise ValueError("perspective_count must be at least 1")
    return MAX_TOLERATED_RISKS * perspective_count - 1


def structural_rubric_scores(
    structural: StructuralCheckReport, falsifiability: FalsifiabilityReport
) -> tuple[bool, RubricItemScore, RubricItemScore]:
    """产出 pre_gate 与 light_hard_gate 共用的前两项 rubric（evidence_traceable /
    falsifiable），外加 evidence_traceable 这个派生判定本身（它不是报告上的现成字段，是
    两个布尔的合取）。

    两个门必须对同一份报告给出完全一致的评分与证据文案，写两遍会在后续改动时悄悄分叉，
    因此收敛到一处。

    Example:
        >>> ok, traceable, falsifiable = structural_rubric_scores(structural, falsifiability)  # doctest: +SKIP
        >>> traceable.item
        'evidence_traceable'
    """
    evidence_traceable_ok = (
        structural.premise_evidence_ok and structural.novel_hypothesis_testable
    )
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


def perspective_ok(review: SkepticReport) -> bool:
    """单个视角是否通过：失败的审阅一律不算通过（fail-closed）——审阅没跑成不能等于审阅批准。

    Example:
        >>> perspective_ok(SkepticReport(idea_id="i", perspective="methodology",
        ...     critique="c", unaddressed_risks=[], fatal_flaw_found=False))
        True
    """
    return (
        not review.failed
        and not review.fatal_flaw_found
        and len(review.unaddressed_risks) <= MAX_TOLERATED_RISKS
    )


def pre_gate(
    structural: StructuralCheckReport, falsifiability: FalsifiabilityReport
) -> GateDecision:
    """依据 StructuralCheckReport 与 FalsifiabilityReport 产出 pre_gate 阶段的 GateDecision。

    Example:
        >>> pre_gate(structural_report, falsifiability_report).verdict  # doctest: +SKIP
        <GateVerdict.PASS: 'PASS'>
    """
    if structural.idea_id != falsifiability.idea_id:
        raise ValueError(
            "structural and falsifiability reports refer to different ideas"
        )

    evidence_traceable_ok, evidence_traceable_score, falsifiable_score = (
        structural_rubric_scores(structural, falsifiability)
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


def light_hard_gate(
    structural: StructuralCheckReport,
    falsifiability: FalsifiabilityReport,
    reviews: list[SkepticReport],
    validation_plan: ValidationPlan,
) -> GateDecision:
    """依据结构/可证伪性报告、每视角一份 review、验证方案产出终审 GateDecision。

    判定优先级（从上到下短路，blocking_factor 记第一个拦住它的项）：
    1. 结构/可证伪性问题 -> REVISE（设计上可修正）
    2. 任一视角 fatal_flaw_found -> REJECT
    3. 任一视角 failed 或风险超阈值 -> REVISE
    4. 跨视角风险总数超 max_total_risks(视角数) -> REVISE
    5. 无可用 verifier -> EXPLORATORY
    6. 全过 -> PASS

    Example:
        >>> light_hard_gate(structural, falsifiability, reviews, plan).gate_phase  # doctest: +SKIP
        'full'
    """
    perspective_ids = [r.perspective for r in reviews]
    if len(perspective_ids) != len(set(perspective_ids)):
        raise ValueError(
            f"light_hard_gate requires distinct perspectives, got {perspective_ids}"
        )

    idea_ids = {structural.idea_id, falsifiability.idea_id, validation_plan.idea_id} | {
        r.idea_id for r in reviews
    }
    if len(idea_ids) != 1:
        raise ValueError(f"reports refer to different ideas: {sorted(idea_ids)}")
    idea_id = structural.idea_id

    evidence_traceable_ok, evidence_traceable_score, falsifiable_score = (
        structural_rubric_scores(structural, falsifiability)
    )

    ordered = sorted(reviews, key=lambda r: r.perspective)
    total_risks = sum(len(r.unaddressed_risks) for r in ordered)
    failed_count = sum(1 for r in ordered if r.failed)
    total_note = f"cross-perspective total unaddressed risks: {total_risks}"

    risk_scores = [
        RubricItemScore(
            item=f"risk_ok_{review.perspective}",
            score=1.0 if perspective_ok(review) else 0.0,
            evidence=(
                f"failed={review.failed}; fatal_flaw_found={review.fatal_flaw_found}; "
                f"unaddressed_risks={len(review.unaddressed_risks)} "
                f"(tolerance {MAX_TOLERATED_RISKS}): {review.unaddressed_risks}; {total_note}"
            ),
        )
        for review in ordered
    ]
    ceiling = max_total_risks(len(ordered))
    total_ok = total_risks <= ceiling
    incomplete_note = (
        f" (incomplete: {failed_count} perspective(s) failed)" if failed_count else ""
    )
    risk_total_score = RubricItemScore(
        item="risk_total",
        score=1.0 if total_ok else 0.0,
        evidence=f"{total_note} (ceiling {ceiling}){incomplete_note}",
    )

    verifier_ok = validation_plan.verifier is not None
    verifier_score = RubricItemScore(
        item="verifier_ok",
        score=1.0 if verifier_ok else 0.0,
        evidence=(
            f"verifier={validation_plan.verifier.verifier_type}"
            if verifier_ok
            else "no verifier matched; validation plan is EXPLORATORY"
        ),
    )

    item_scores = [
        evidence_traceable_score,
        falsifiable_score,
        risk_total_score,
        verifier_score,
        *risk_scores,
    ]

    fatal = next((r for r in ordered if r.fatal_flaw_found), None)
    blocked = next((r for r in ordered if not perspective_ok(r)), None)

    if not evidence_traceable_ok or not falsifiability.is_falsifiable:
        verdict = GateVerdict.REVISE
        blocking_factor = (
            "evidence_traceable" if not evidence_traceable_ok else "falsifiable"
        )
    elif fatal is not None:
        verdict, blocking_factor = GateVerdict.REJECT, f"risk_ok_{fatal.perspective}"
    elif blocked is not None:
        verdict, blocking_factor = GateVerdict.REVISE, f"risk_ok_{blocked.perspective}"
    elif not total_ok:
        verdict, blocking_factor = GateVerdict.REVISE, "risk_total"
    elif not verifier_ok:
        verdict, blocking_factor = GateVerdict.EXPLORATORY, "verifier_ok"
    else:
        verdict, blocking_factor = GateVerdict.PASS, None

    return GateDecision(
        idea_id=idea_id,
        gate_phase="full",
        verdict=verdict,
        rubric_version=GATE_RUBRIC_VERSION,
        item_scores=item_scores,
        blocking_factor=blocking_factor,
    )
