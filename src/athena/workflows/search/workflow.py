"""Idea Generation 的编排层：整条链路的唯一入口都在这一个文件里。

对外两个入口：
- ``run_pre_gate``（P0 遗留）：单策略生成 -> structural_check -> falsifiability_check ->
  pre_gate，只跑到步骤 [4]。保留它是因为 ``demo_pre_gate.py`` 与既有测试仍以它作为最小闭环
  演示入口；P1+P2 的正式路径是 ``run_full_pipeline``。
- ``run_full_pipeline``（P1+P2）：空白挖掘 [2] -> 多候选生成+去重 [3] -> pre_gate [4] ->
  数值性审计 [5] -> 三视角审阅委员会 [6]（methodology/statistics/domain_consistency 并行）
  -> 验证方案 [7] -> hard_gate [8] -> 存活候选（PASS/EXPLORATORY）pairwise Elo 排序 [9]。

编排原则（延续此前 review 意见"整条链路应该是一个整体"）：生成/审计/门控不散落到多处调用
点；原先用 langgraph StateGraph 承载，依赖栈切到 openai + pydantic-ai 后一度改为纯异步顺序
调用，现在编排又换回了 ``graph.py`` 里的 langgraph 图——承载手段几经变化，但"一个整体"的
约束不变：``run_full_pipeline`` 仍是唯一入口。[4]-[9]（含修订闭环与 pairwise 排序）的领域
逻辑已经随图编排改造迁到 ``graph.py`` 的各节点（``screen_node``/.../``regate_node``/
``rank_node``）与 ``graph.py`` 自己的 ``pairwise_compare`` 里，本文件不再保留它们——
``graph.py`` 文档字符串开头声明"本文件是唯一允许 import langgraph 的业务文件"，本文件因此
只在类型注解位置以 ``TYPE_CHECKING`` 方式引用 langgraph 的类型，不在运行时 import 它。

Agent 的使用边界遵循 Occam's razor：只有真正需要工具权限+多轮检索的步骤才走
``core.agent.Agent``——[2] 空白挖掘、[5] 数值性审计（见 evidence_retrieval.py），以及 [6]
审阅委员会里唯一配了 paper_rag 工具、需要跑检索循环核对领域一致性的 domain_consistency
视角（见 review_board.py 的 ``build_domain_consistency_agent``/``review_one_perspective``）。
其余单次结构化生成——[3] 候选生成、[6] 里仅靠包内信息即可判定的 methodology/statistics
两个视角、以及 [9] pairwise 比较——都走 ``single_turn_chat``，不接 ThreadManager/BaseAgent。

并发编排（本分支的另一大动因）：``run_full_pipeline`` 内部按 LLM_CONCURRENCY /
RETRIEVAL_CONCURRENCY 建两级 ``asyncio.Semaphore``（下面这两个常量），分别约束
single_turn_chat 调用与带检索的 Agent 循环，随 ``PipelineDeps`` 一起转交给 ``graph.py``。
名额获取与释放的粒度、以及死锁规避规则见两个常量与 ``graph.py`` 各节点各自的函数注释。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic_ai.models import Model

from athena.core.agent import Agent
from athena.core.schemas import ArtifactRef, Hypothesis
from athena.research.ranking import RankedCandidate
from athena.storage.artifact_store import ArtifactStore
from athena.utils.single_turn_chat import single_turn_chat
from athena.workflows.prompts import (
    IDEA_GENERATOR_SYSTEM_PROMPT,
    IDEA_GENERATOR_USER_PROMPT_TEMPLATE,
)
from athena.workflows.search.candidate_generation import MAX_VERBALIZED_SAMPLES
from athena.workflows.search.gatekeeper import pre_gate
from athena.workflows.search.graph import run_graph
from athena.workflows.search.idea_schemas import (
    GateDecision,
    HypothesisDraft,
    HypothesisPackage,
    PipelineCandidateResult,
    ResearchProblemInput,
)
from athena.workflows.search.pre_gate_checks import falsifiability_check, structural_check
from athena.workflows.search.state import PipelineDeps

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver


# ====== 常量 ======

GENERATION_STRATEGY: str = "single_strategy_v1"
MAX_GENERATION_ATTEMPTS: int = 2

LLM_CONCURRENCY: int = 16
"""single_turn_chat 的并发上限。

按 **pairwise 阶段**定的——那是全流水线唯一有确定并发上界的阶段：n 个存活候选 = C(n,2) 对,
每对双向 2 次且并发,5 个候选就是 20 次同时在飞。审阅阶段峰值与之同量级(3 个视角调用 +
novelty 与 domain_consistency 各自的转结构化调用),但随视角数与候选数两个变量浮动,不适合
作为定值依据。日后调 MAX_VERBALIZED_SAMPLES 或增删视角时,该动的是这条注释描述的推算,
不是随手改数字。"""

RETRIEVAL_CONCURRENCY: int = 4
"""带 paper_rag 工具的 Agent 检索循环的并发上限。保守估计,未经 paper_rag 实际承受能力验证。

**名额必须在单个检索循环的粒度上获取与释放**,禁止跨越 novelty -> domain_consistency 的
依赖边界持有:持有一个名额的同时等待第二个名额,在候选数 >= 名额数时必然死锁(5 个候选抢
4 个名额,4 个各持 1 个、各等 1 个)。只用 async with 获取,不手动 acquire()/release()——
异常路径漏 release 会静默泄漏名额,症状是流水线越跑越慢直至卡死,极难定位。"""


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


# ====== 全流程编排（P1+P2） ======

async def run_full_pipeline(
    problem: ResearchProblemInput,
    *,
    gap_miner_agent: Agent,
    novelty_agent: Agent,
    domain_review_agent: Agent,
    artifacts: ArtifactStore,
    corpus_ref: ArtifactRef,
    sample_size: int = MAX_VERBALIZED_SAMPLES,
    model: Model | str | None = None,
    thread_id: str | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    emit: Callable[..., Awaitable[None]] | None = None,
) -> tuple[list[PipelineCandidateResult], list[RankedCandidate]]:
    """P1+P2 全流程：[2]空白挖掘 → [3]多候选生成+去重 → 每个候选跑
    [4]pre_gate(不合格直接筛掉,跳过[5]-[8]) → [5]数值性审计 → [6]三视角审阅委员会 →
    [7]验证方案 → [8]hard_gate → [9]存活候选(PASS/EXPLORATORY) pairwise Elo 排序。

    gap_miner_agent 与 novelty_agent 是两个独立的 core.agent.Agent 实例(各自配了不同的
    system_prompt与各自的 agent.config.tools),分别用 evidence_retrieval.build_gap_miner_agent
    与 build_novelty_agent 构造,以保证绑定的是 GAP_MINER_SYSTEM_PROMPT/NOVELTY_SYSTEM_PROMPT；
    domain_review_agent 同理由 review_board.build_domain_consistency_agent 构造,供 [6] 的
    domain_consistency 视角使用；ResearchTree 交接(add_hypothesis)不在本轮范围内。

    编排已换成 ``graph.py`` 里的 langgraph 图（gap_mining → generate → Send fan-out →
    collect → rank），本函数只负责组装一次性的 PipelineDeps 并转交，是个薄壳。

    thread_id/checkpointer 原样透传给 run_graph（Task 6）：都为 None 时不启用 checkpoint，
    行为与重构前一致；同时提供才会在中断后按 thread_id 续跑，跳过已完成的节点。

    emit 同样原样透传：为 None（默认）时整条链路不发任何事件，行为与不带事件时逐字节一致；
    非 None 时 run_graph 在顶层节点边界发 idea_generation/started|step|completed，候选子图
    另发带 candidate_index 的候选级 step 事件。**本参数存在的理由是"唯一入口"这条约束**——
    事件桥接不能只在 run_graph 上可用，否则调用方要拿到候选级事件就必须绕过本函数、自己
    组装 PipelineDeps，而组装 PipelineDeps 正是本函数存在的意义。事件最终挂到
    ThreadManager 还是别处由 execution/app_server 决定（见 CLAUDE.md"四种执行形态分层未定"），
    本函数只负责把回调透传下去，不假设消费者是谁。

    Example:
        >>> results, ranking = await run_full_pipeline(problem, gap_miner_agent=agent1,
        ...     novelty_agent=agent2, domain_review_agent=agent3, artifacts=store,
        ...     corpus_ref=corpus_ref, model=fake_model)  # doctest: +SKIP
    """
    deps = PipelineDeps(
        artifacts=artifacts, corpus_ref=corpus_ref,
        llm_sem=asyncio.Semaphore(LLM_CONCURRENCY),
        retrieval_sem=asyncio.Semaphore(RETRIEVAL_CONCURRENCY),
        gap_miner_agent=gap_miner_agent, novelty_agent=novelty_agent,
        domain_review_agent=domain_review_agent, model=model,
    )
    return await run_graph(problem, deps=deps, sample_size=sample_size,
                            thread_id=thread_id, checkpointer=checkpointer, emit=emit)
