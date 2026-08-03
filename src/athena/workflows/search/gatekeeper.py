"""gatekeeper：整条 Idea Generation 流水线里唯一产出 GateVerdict 的地方。

两个门：
- pre_gate（步骤 [4]，2 项 rubric）：只看结构完整性与可证伪性，任一不满足 -> REVISE。它是
  廉价前置筛子，跑在昂贵的检索/审阅步骤之前，不合格的候选直接淘汰。
- hard_gate（步骤 [8]，5 + len(REVIEW_PERSPECTIVES) 项 rubric）：在 pre_gate 两项之上追加
  novelty_ok/verifier_ok/risk_total，外加每个审阅视角一项 risk_ok_<perspective>，产出
  gate_phase="full" 的终审判决。

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
from athena.workflows.search.review_board import REVIEW_PERSPECTIVES


# ====== 常量 ======

NOVELTY_OVERLAP_THRESHOLD: float = 0.7
"""facet_overlap 均值达到或超过此阈值,判定为与已有工作重叠过高(不够新颖)。"""

MAX_TOLERATED_RISKS: int = 2
"""hard_gate 容忍的"未处理风险"条数上限；反方审阅几乎总能挑出一两条风险,若一条就拦下来,
没有候选能走到步骤 [9] 排序。与 NOVELTY_OVERLAP_THRESHOLD 同理,用阈值而非布尔量判定。"""

MAX_TOTAL_RISKS: int = MAX_TOLERATED_RISKS + 1
"""跨视角未处理风险总量上限,堵住"把风险摊到多个视角上规避门控"这条回归。

**取值必须严格小于 MAX_TOLERATED_RISKS * len(REVIEW_PERSPECTIVES),否则这一项是死代码。**
判定链第 5 条(任一视角超单项阈值)先于第 6 条,所以第 6 条要触发就必须"所有视角都不超单项
阈值、但总量超标";若本值 >= 单项阈值 × N,这两个条件数学上无法同时成立(所有视角 <= T
蕴含总量 <= T×N),risk_total 就成了恒满分、永不作为 blocking_factor 的摆设。

取 MAX_TOLERATED_RISKS + 1 = 3 让判定语义真正回到拆分之前:拆分前一份报告超过 2 条就
REVISE,现在跨视角超过 2 条同样 REVISE。代价是这比拆分前**更容易触发**——三个聚焦审阅者
共同提出 3 条风险很常见,而拆分前那个通才审阅者未必会把同样 3 条都列出来。本值与
MAX_TOLERATED_RISKS 一样没有经验依据,是"先堵住已知回归、宁严勿宽"的起点值,等真实跑过
几轮能看到候选通过率之后两者一并校准。调整时务必保持上面那条不等式。"""


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


def perspective_ok(review: SkepticReport) -> bool:
    """单个视角是否通过：失败的审阅一律不算通过（fail-closed）——审阅没跑成不能等于审阅批准。

    Example:
        >>> perspective_ok(SkepticReport(idea_id="i", perspective="methodology",
        ...     critique="c", unaddressed_risks=[], fatal_flaw_found=False))
        True
    """
    return (not review.failed
            and not review.fatal_flaw_found
            and len(review.unaddressed_risks) <= MAX_TOLERATED_RISKS)


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
    reviews: list[SkepticReport],
    validation_plan: ValidationPlan,
) -> GateDecision:
    """依据 4 份报告 + 每视角一份 review 产出 hard_gate 阶段的 GateDecision(gate_phase="full")。

    判定优先级（从上到下短路，blocking_factor 记第一个拦住它的项）：
    1. 结构/可证伪性问题 -> REVISE（设计上可修正）
    2. facet_overlap 为空（新颖性缺证据）-> REVISE
    3. facet_overlap 均值超阈值 -> REJECT
    4. 任一视角 fatal_flaw_found -> REJECT
    5. 任一视角 failed 或风险超阈值 -> REVISE
    6. 跨视角风险总数超 MAX_TOTAL_RISKS -> REVISE
    7. 无可用 verifier -> EXPLORATORY
    8. 全过 -> PASS

    第 4/5 步按 REVIEW_PERSPECTIVES 的顺序扫描,且 reviews 在函数内部重排——把"按视角顺序"
    这个确定性承诺寄托在调用方（asyncio.gather 的入参顺序）身上等于没有承诺。

    Example:
        >>> hard_gate(structural, falsifiability, novelty, reviews, plan).gate_phase  # doctest: +SKIP
        'full'
    """
    expected_perspectives = {p.perspective_id for p in REVIEW_PERSPECTIVES}
    got_perspectives = {r.perspective for r in reviews}
    # 集合相等只保证"覆盖到的视角种类对"，不保证"每个视角恰好一份"——比如传两份 methodology
    # 加各一份其余视角，集合比较照样通过，随后 by_perspective 字典推导会静默丢弃前一份
    # methodology，把它的风险/fatal 标记吃掉。长度检查把"exactly one"落到实处。
    if got_perspectives != expected_perspectives or len(reviews) != len(REVIEW_PERSPECTIVES):
        raise ValueError(
            "hard_gate requires exactly one review per perspective; expected "
            f"{sorted(expected_perspectives)}, got {len(reviews)} review(s) for "
            f"{sorted(got_perspectives)}"
        )

    idea_ids = {structural.idea_id, falsifiability.idea_id, novelty.idea_id,
                validation_plan.idea_id} | {r.idea_id for r in reviews}
    if len(idea_ids) != 1:
        raise ValueError(f"reports refer to different ideas: {sorted(idea_ids)}")
    idea_id = structural.idea_id

    evidence_traceable_ok, evidence_traceable_score, falsifiable_score = structural_rubric_scores(
        structural, falsifiability
    )

    # facet_overlap 为空 = 检索侧一项重叠度都没打出来，此时"新颖"是没有证据支撑的默认值，
    # 不能当成通过——否则均值 0.0 会让"什么都没查到"自动拿满分。这种情况判 REVISE。
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

    by_perspective = {r.perspective: r for r in reviews}
    ordered = [by_perspective[p.perspective_id] for p in REVIEW_PERSPECTIVES]
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
    total_ok = total_risks <= MAX_TOTAL_RISKS
    incomplete_note = (
        f" (incomplete: {failed_count} perspective(s) failed)" if failed_count else ""
    )
    risk_total_score = RubricItemScore(
        item="risk_total", score=1.0 if total_ok else 0.0,
        evidence=f"{total_note} (ceiling {MAX_TOTAL_RISKS}){incomplete_note}",
    )

    item_scores = [evidence_traceable_score, falsifiable_score, novelty_score,
                   verifier_score, risk_total_score, *risk_scores]

    fatal = next((r for r in ordered if r.fatal_flaw_found), None)
    blocked = next((r for r in ordered if not perspective_ok(r)), None)

    if not evidence_traceable_ok or not falsifiability.is_falsifiable:
        verdict = GateVerdict.REVISE
        blocking_factor = "evidence_traceable" if not evidence_traceable_ok else "falsifiable"
    elif novelty_evidence_missing:
        verdict, blocking_factor = GateVerdict.REVISE, "novelty_ok"
    elif not novelty_ok:
        verdict, blocking_factor = GateVerdict.REJECT, "novelty_ok"
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
        idea_id=idea_id, gate_phase="full", verdict=verdict,
        rubric_version=GATE_RUBRIC_VERSION, item_scores=item_scores,
        blocking_factor=blocking_factor,
    )
