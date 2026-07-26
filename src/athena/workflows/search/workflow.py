"""Idea Generation P0（pre_gate 闭环）的完整异步流水线：generate -> structural_check ->
falsifiability_check -> gate。

架构变更说明：原实现用 langgraph 的 StateGraph 把这四步接成一张 CompiledGraph（响应此前
review 反馈"不应该是分布式 CompiledGraph"）；随着共享依赖栈从 langchain/langgraph 切换到
openai + pydantic-ai（见 pyproject.toml），继续依赖 langgraph 已不可行，四步改回顺序 await
的纯异步调用。但仍然只集中在这一个文件里对外暴露唯一入口 run_pre_gate，不把编排逻辑散到
多处调用点——这是对原 review 意见"整条链路应该是一个整体"的延续，只是承载手段从"图"换成了
"一个函数里的顺序 await"。

P0 说明：不经过 app_server 的 ThreadManager/Agent 循环——那一套（core/agent/agent.py）是为
工具调用型 ReAct Agent 设计的（多轮采样、工具执行、并发工具调用）；我们这里是单次结构化生成，
不需要工具、不需要多轮对话，接入 ThreadManager/BaseAgent 属于过度设计，遵循 Occam's razor：
只在真正需要独立上下文、工具权限、多轮推理或并发时才用 Agent。
"""

import uuid

from pydantic_ai.models import Model

from athena.core.schemas import Hypothesis
from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import IDEA_GENERATOR_SYSTEM_PROMPT, IDEA_GENERATOR_USER_PROMPT_TEMPLATE
from athena.workflows.search.gatekeeper import pre_gate
from athena.workflows.search.idea_schemas import (
    GateDecision,
    HypothesisDraft,
    HypothesisPackage,
    ResearchProblemInput,
)
from athena.workflows.search.pre_gate_checks import falsifiability_check, structural_check


# ====== 常量 ======

GENERATION_STRATEGY: str = "single_strategy_v1"
MAX_GENERATION_ATTEMPTS: int = 2


# ====== 对外入口 ======

async def run_pre_gate(
    problem: ResearchProblemInput, *, model: Model | str | None = None
) -> tuple[Hypothesis, HypothesisPackage, GateDecision]:
    """跑一遍 [3]→[4] 的 P0 闭环，返回 (Hypothesis, HypothesisPackage, GateDecision)。

    设计参考：生成阶段本身不做自我批判/自我打分（打分权始终在 gatekeeper.pre_gate 一处），
    呼应 Co-Scientist 论文里"生成与审阅分离，生成侧不自证"的思路（AI co-scientist,
    arXiv:2502.18864）；P0 只跑单一策略，多策略并行生成/空白挖掘留给后续迭代。

    Example:
        >>> node, package, decision = await run_pre_gate(problem, model=fake_model)  # doctest: +SKIP
        >>> decision.gate_phase
        'pre_gate'
    """
    idea_id = f"idea-{uuid.uuid4().hex[:12]}"

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
            draft = await single_turn_chat(prompt, HypothesisDraft, model=model)
            break
        except Exception as error:  # noqa: BLE001 - provider 报错形态不定，统一重试一次后再上抛
            last_error = error
    if draft is None:
        raise ValueError(f"IdeaGenerator failed to produce a valid HypothesisDraft: {last_error}")

    # 共享 schema 的 Hypothesis 没有 node_id/package_ref 这类身份字段了（RecordNode 已被移除），
    # 只承载内容；idea_id 只在本模块自己的 HypothesisPackage 里作为审计/关联用的标识
    node = Hypothesis(
        statement=draft.statement,
        intervention=draft.intervention,
        expected_effect=draft.expected_effect,
        evidence_refs=[ref for premise in draft.supported_premises for ref in premise.supporting_refs],
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

    structural_report = structural_check(package)
    falsifiability_report = await falsifiability_check(package, model=model)
    decision = pre_gate(structural_report, falsifiability_report)
    return node, package, decision
