"""IdeaGenerator：单策略假设生成 Agent（步骤 [3]）。

P0 说明：这里直接用 langgraph 的单节点 StateGraph 承载生成逻辑，不经过 ThreadManager.submit()——
ThreadManager 尚未实现，这是临时决定；ThreadManager 落地后需要在它之上补一层适配，而不是假装
已经兼容。图内只有一个真实节点，为后续加 mine_gap/dedupe/多策略节点预留位置，但 P0 不实现它们。
节点内部只发生一次模型调用，解析失败在节点内重试一次，再失败就向上抛出 ValueError（不做无限
重试，不吞异常）。
"""

import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from athena.core.schemas import Hypothesis
from athena.utils.single_turn_chat import StructuredChatModel, single_turn_chat
from athena.workflows.prompts import IDEA_GENERATOR_SYSTEM_PROMPT, IDEA_GENERATOR_USER_PROMPT_TEMPLATE
from athena.workflows.search.idea_schemas import HypothesisDraft, HypothesisPackage, ResearchProblemInput


# ====== 常量 ======

GENERATION_STRATEGY: str = "single_strategy_v1"
MAX_GENERATION_ATTEMPTS: int = 2


# ====== 类型 ======

class _GraphState(TypedDict):
    """langgraph 单节点图的状态；draft 在 generate 节点写入，不做序列化/持久化。"""
    problem: ResearchProblemInput
    model: StructuredChatModel | None
    draft: HypothesisDraft | None


# ====== 功能代码 ======

async def _generate_node(state: _GraphState) -> dict:
    """langgraph 节点：拼 prompt、调一次 LLM，解析失败重试一次，仍失败则抛出 ValueError。"""
    problem = state["problem"]
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

    last_error: Exception | None = None
    for _ in range(MAX_GENERATION_ATTEMPTS):
        try:
            draft = await single_turn_chat(prompt, HypothesisDraft, model=state["model"])
            return {"draft": draft}
        except Exception as error:  # noqa: BLE001 - provider 报错形态不定，统一重试一次后再上抛
            last_error = error
    raise ValueError(f"IdeaGenerator failed to produce a valid HypothesisDraft: {last_error}")


async def generate_hypothesis(
    problem: ResearchProblemInput, *, model: StructuredChatModel | None = None
) -> tuple[Hypothesis, HypothesisPackage]:
    """IdeaGenerator 对外入口：跑一遍单节点 langgraph 图，返回 (Hypothesis 节点, HypothesisPackage)。

    Example:
        >>> node, package = await generate_hypothesis(problem, model=fake_model)  # doctest: +SKIP
        >>> node.node_id == package.idea_id
        True
    """
    idea_id = f"idea-{uuid.uuid4().hex[:12]}"

    graph = StateGraph(_GraphState)
    graph.add_node("generate", _generate_node)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    compiled = graph.compile()

    result = await compiled.ainvoke({"problem": problem, "model": model, "draft": None})
    draft: HypothesisDraft = result["draft"]

    # 把 LLM 输出的 draft 折成 Athena RecordNode 视图；P0 用 inline:// 占位 payload_ref/package_ref，
    # 等 ArtifactStore 落地后替换成真实引用
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
    return node, package
