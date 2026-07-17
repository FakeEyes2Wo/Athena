"""run_pre_gate：纯编排函数，串联步骤 [3]→[4]（生成 → 结构/可证伪审计 → pre_gate）。

不是 Service 类、也不是 Agent，只是一个 async 函数——P0 阶段不维护 RunState/IdeaState 状态机，
也不做持久化，调用方自己决定要不要重跑。
"""

from athena.agents.search.research_agent import generate_hypothesis
from athena.core.schemas import Hypothesis
from athena.utils.single_turn_chat import StructuredChatModel
from athena.workflows.search.gatekeeper import pre_gate
from athena.workflows.search.idea_schemas import (
    GateDecision,
    GateVerdict,
    HypothesisPackage,
    ResearchProblemInput,
)
from athena.workflows.search.pre_gate_checks import falsifiability_check, structural_check


# pre_gate 的 verdict 到 Hypothesis 节点 status 的映射，字符串对应设计文档 IdeaState 的命名，
# 但 P0 不接入完整的 IdeaState 状态机，这里只是标记字符串，供 demo 输出参考
_STATUS_BY_VERDICT: dict[GateVerdict, str] = {
    GateVerdict.PASS: "GATED_PASS",
    GateVerdict.REVISE: "REVISION_REQUESTED",
    GateVerdict.REJECT: "REJECTED",
}


async def run_pre_gate(
    problem: ResearchProblemInput, *, model: StructuredChatModel | None = None
) -> tuple[Hypothesis, HypothesisPackage, GateDecision]:
    """跑一遍 [1]→[4] 的 P0 闭环，返回 (Hypothesis 节点, HypothesisPackage, GateDecision)。

    Example:
        >>> node, package, decision = await run_pre_gate(problem, model=fake_model)  # doctest: +SKIP
        >>> decision.gate_phase
        'pre_gate'
    """
    node, package = await generate_hypothesis(problem, model=model)
    structural_report = structural_check(package)
    falsifiability_report = await falsifiability_check(package, model=model)
    decision = pre_gate(structural_report, falsifiability_report)
    node.status = _STATUS_BY_VERDICT.get(decision.verdict, "DRAFTED")
    return node, package, decision
