"""步骤 [4] 的两项审计：均只产出报告，不产出 verdict（判断权集中在 gatekeeper）。两份报告
既喂给 pre_gate 做廉价前置筛选，也作为 hard_gate 五项 rubric 里的前两项。"""

from pydantic_ai.models import Model

from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    FALSIFIABILITY_CHECK_SYSTEM_PROMPT,
    FALSIFIABILITY_CHECK_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.idea_schemas import (
    ClaimRole,
    FalsifiabilityJudgment,
    FalsifiabilityReport,
    HypothesisPackage,
    StructuralCheckReport,
)


def structural_check(package: HypothesisPackage) -> StructuralCheckReport:
    """检查 HypothesisPackage 的结构完整性。这两条不变量已经被 Pydantic validator 在构造时
    强制保证，本函数把它们落成可审计的报告对象，供 gatekeeper 和 AuditTrail 使用。

    Example:
        >>> structural_check(package).premise_evidence_ok  # doctest: +SKIP
        True
    """
    violations: list[str] = []

    # 只审计 SUPPORTED_PREMISE 角色的 claim；其他角色（如 DERIVED_INFERENCE）没有绑证据的
    # 不变量，混进来检查会误判。零条 SUPPORTED_PREMISE 时 all([]) 恒真，必须显式排除这种
    # "零证据也算通过"的空洞情况。
    premises = [
        claim for claim in package.supported_premises if claim.role == ClaimRole.SUPPORTED_PREMISE
    ]
    premise_evidence_ok = bool(premises) and all(claim.supporting_refs for claim in premises)
    if not premise_evidence_ok:
        violations.append("premise_missing_evidence")

    novel_hypothesis_testable = bool(
        package.predicted_observations and package.disconfirming_observations
    )
    if not novel_hypothesis_testable:
        violations.append("missing_predictions_or_disconfirmers")

    return StructuralCheckReport(
        idea_id=package.idea_id,
        premise_evidence_ok=premise_evidence_ok,
        novel_hypothesis_testable=novel_hypothesis_testable,
        violations=violations,
    )


def degraded_falsifiability_report(idea_id: str, error: Exception) -> FalsifiabilityReport:
    """falsifiability_check 失败时的降级报告：没有证据不能算通过，交给 gatekeeper 判
    REVISE，不静默放行。两处调用点（graph.screen_node 前置筛选、
    revision.refresh_stale_evidence 终局刷新）的降级行为必须逐字节一致，故抽成一处。

    Example:
        >>> degraded_falsifiability_report(
        ...     "idea-1", RuntimeError("down")).is_falsifiable  # doctest: +SKIP
        False
    """
    return FalsifiabilityReport(
        idea_id=idea_id, testable_implication="",
        unobservable_variables=[f"falsifiability check failed: {error}"],
        is_falsifiable=False,
    )


async def falsifiability_check(
    package: HypothesisPackage, *, model: Model | str | None = None
) -> FalsifiabilityReport:
    """用一次单轮 LLM 调用判断核心变量是否可观测、是否存在可执行的可证伪测试。

    Example:
        >>> report = await falsifiability_check(package)  # doctest: +SKIP
        >>> report.is_falsifiable
        True
    """
    prompt = "\n\n".join([
        FALSIFIABILITY_CHECK_SYSTEM_PROMPT,
        FALSIFIABILITY_CHECK_USER_PROMPT_TEMPLATE.format(
            novel_hypothesis=package.novel_hypothesis,
            predicted_observations="\n".join(f"- {obs}" for obs in package.predicted_observations),
            disconfirming_observations="\n".join(
                f"- {obs}" for obs in package.disconfirming_observations
            ),
        ),
    ])
    judgment = await single_turn_chat(prompt, FalsifiabilityJudgment, model=model)
    return FalsifiabilityReport(
        idea_id=package.idea_id,
        testable_implication=judgment.testable_implication,
        unobservable_variables=judgment.unobservable_variables,
        is_falsifiable=judgment.is_falsifiable,
    )
