"""Idea Generation P0（pre_gate 闭环）的完整 langgraph 工作流。

审查反馈：不应该是"分布式 CompiledGraph"（一个 langgraph 节点 + 外部普通函数依次调用），
整条 [3]→[4] 链路（生成 → 结构审计 → 可证伪性审计 → 门控）都应该是同一张 StateGraph 上的节点，
用 langgraph 的 add_node/add_edge 表达，而不是在图外面再串一层 Python 调用。本文件是这条链路
唯一的 CompiledGraph 入口；`pre_gate_checks.py`/`gatekeeper.py` 仍保留纯函数实现（职责单一、
独立可测），本文件只把它们包装成图节点。

P0 说明：不经过 ThreadManager.submit()——ThreadManager 尚未实现，这是临时决定；ThreadManager
落地后需要在它之上补一层适配，而不是假装已经兼容。四个节点线性连接（不做条件边短路），
保证行为与门控语义（pre_gate 始终需要两份报告）完全一致，不引入额外的分支复杂度。
"""

import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from athena.core.schemas import Hypothesis
from athena.utils.single_turn_chat import StructuredChatModel, single_turn_chat
from athena.workflows.prompts import IDEA_GENERATOR_SYSTEM_PROMPT, IDEA_GENERATOR_USER_PROMPT_TEMPLATE
from athena.workflows.search.gatekeeper import pre_gate
from athena.workflows.search.idea_schemas import (
    FalsifiabilityReport,
    GateDecision,
    GateVerdict,
    HypothesisDraft,
    HypothesisPackage,
    ResearchProblemInput,
    StructuralCheckReport,
)
from athena.workflows.search.pre_gate_checks import falsifiability_check, structural_check


# ====== 常量 ======

GENERATION_STRATEGY: str = "single_strategy_v1"
MAX_GENERATION_ATTEMPTS: int = 2

# pre_gate 的 verdict 到 Hypothesis 节点 status 的映射，字符串对应设计文档 IdeaState 的命名，
# 但 P0 不接入完整的 IdeaState 状态机，这里只是标记字符串，供 demo 输出参考
_STATUS_BY_VERDICT: dict[GateVerdict, str] = {
    GateVerdict.PASS: "GATED_PASS",
    GateVerdict.REVISE: "REVISION_REQUESTED",
    GateVerdict.REJECT: "REJECTED",
}


# ====== 类型 ======

class PreGateState(TypedDict):
    """pre_gate 闭环全流程共用的图状态；不做序列化/持久化，只在单次 ainvoke 内传递。"""
    problem: ResearchProblemInput
    model: StructuredChatModel | None
    idea_id: str
    node: Hypothesis | None
    package: HypothesisPackage | None
    structural_report: StructuralCheckReport | None
    falsifiability_report: FalsifiabilityReport | None
    decision: GateDecision | None


# ====== 图节点 ======

async def _generate_node(state: PreGateState) -> dict:
    """节点 [3]：拼 prompt、调一次 LLM，解析失败重试一次，仍失败则抛出 ValueError；
    再把 LLM 输出的 draft 折成 Hypothesis 节点与 HypothesisPackage。
    """
    problem = state["problem"]
    idea_id = state["idea_id"]
    evidence_lines = "\n".join(
        f"ev-{index}: {text}" for index, text in enumerate(problem.evidence_texts)
    ) or "(no evidence supplied)"
    constraint_lines = "\n".join(f"- {c}" for c in problem.constraints) or "(none)"
    user_prompt = IDEA_GENERATOR_USER_PROMPT_TEMPLATE.format(
        question=problem.question,
        domain=problem.domain,
        objective=problem.objective,
        constraints=constraint_lines,
        evidence=evidence_lines,
    )
    prompt = f"{IDEA_GENERATOR_SYSTEM_PROMPT}\n\n{user_prompt}"

    draft: HypothesisDraft | None = None
    last_error: Exception | None = None
    for _ in range(MAX_GENERATION_ATTEMPTS):
        try:
            draft = await single_turn_chat(prompt, HypothesisDraft, model=state["model"])
            break
        except Exception as error:  # noqa: BLE001 - provider 报错形态不定，统一重试一次后再上抛
            last_error = error
    if draft is None:
        raise ValueError(f"IdeaGenerator failed to produce a valid HypothesisDraft: {last_error}")

    # P0 用 inline:// 占位 payload_ref/package_ref，等 ArtifactStore 落地后替换成真实引用
    evidence_refs = [ref for premise in draft.supported_premises for ref in premise.supporting_refs]
    node = Hypothesis(
        node_id=idea_id,
        parent_ids=[],
        status="DRAFTED",
        payload_ref=f"inline://{idea_id}",
        statement=draft.statement,
        intervention=draft.intervention,
        expected_effect=draft.expected_effect,
        evidence_refs=evidence_refs,
        package_ref=f"inline://{idea_id}",
    )
    # 补上代码负责的 idea_id/lineage_op/validation_plan_ref；这些字段不该由 LLM 决定
    package = HypothesisPackage(
        idea_id=idea_id,
        generation_strategy=draft.generation_strategy or GENERATION_STRATEGY,
        novel_hypothesis=draft.statement,
        supported_premises=draft.supported_premises,
        inference_chain=draft.inference_chain,
        predicted_observations=draft.predicted_observations,
        disconfirming_observations=draft.disconfirming_observations,
        validation_plan_ref=None,
        lineage_op="generate",
    )
    return {"node": node, "package": package}


def _structural_check_node(state: PreGateState) -> dict:
    """节点 [4a]：纯函数结构审计，包一层适配 graph state。"""
    return {"structural_report": structural_check(state["package"])}


async def _falsifiability_check_node(state: PreGateState) -> dict:
    """节点 [4b]：一次 LLM 判断可证伪性，包一层适配 graph state。"""
    report = await falsifiability_check(state["package"], model=state["model"])
    return {"falsifiability_report": report}


def _gate_node(state: PreGateState) -> dict:
    """节点 [4c]：唯一产出 GateVerdict 的地方，并把 verdict 落到 Hypothesis 节点的 status 上。"""
    decision = pre_gate(state["structural_report"], state["falsifiability_report"])
    node = state["node"]
    node.status = _STATUS_BY_VERDICT.get(decision.verdict, "DRAFTED")
    return {"decision": decision, "node": node}


# ====== 图构建 ======

def _build_graph():
    """构建 pre_gate 闭环唯一的 CompiledGraph：generate -> structural_check ->
    falsifiability_check -> gate，线性连接，不做条件边短路。
    """
    graph = StateGraph(PreGateState)
    graph.add_node("generate", _generate_node)
    graph.add_node("structural_check", _structural_check_node)
    graph.add_node("falsifiability_check", _falsifiability_check_node)
    graph.add_node("gate", _gate_node)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", "structural_check")
    graph.add_edge("structural_check", "falsifiability_check")
    graph.add_edge("falsifiability_check", "gate")
    graph.add_edge("gate", END)
    return graph.compile()


# 模块级单例：全项目共用一份编译好的图，避免每次调用都重新构建
PRE_GATE_GRAPH = _build_graph()


# ====== 对外入口 ======

async def run_pre_gate(
    problem: ResearchProblemInput, *, model: StructuredChatModel | None = None
) -> tuple[Hypothesis, HypothesisPackage, GateDecision]:
    """跑一遍编译好的 pre_gate CompiledGraph，返回 (Hypothesis 节点, HypothesisPackage, GateDecision)。

    Example:
        >>> node, package, decision = await run_pre_gate(problem, model=fake_model)  # doctest: +SKIP
        >>> decision.gate_phase
        'pre_gate'
    """
    idea_id = f"idea-{uuid.uuid4().hex[:12]}"
    result = await PRE_GATE_GRAPH.ainvoke({
        "problem": problem,
        "model": model,
        "idea_id": idea_id,
        "node": None,
        "package": None,
        "structural_report": None,
        "falsifiability_report": None,
        "decision": None,
    })
    return result["node"], result["package"], result["decision"]
