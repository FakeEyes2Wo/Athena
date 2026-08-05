"""图编排的状态与依赖定义。

拆分规则只有一条：**能否序列化**。可序列化的事实进 PipelineState / CandidateState，随
checkpoint 落盘；不可序列化的运行时对象（ArtifactStore 的 I/O 句柄、绑定 event loop 的
Semaphore、持有 provider 的 Agent）进 PipelineDeps，走 langgraph 的 context_schema，天然不进
checkpoint。

这一拆同时解掉两件事：领域函数（revise_candidate/refresh_stale_evidence 等）里那一簇
{artifacts, corpus_ref, llm_sem, retrieval_sem, model} 在多处逐字重复的参数负担；以及
checkpoint 对状态可序列化的硬要求。
"""

import asyncio
import operator
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, TypedDict

from pydantic_ai.models import Model

from athena.core.agent import Agent
from athena.core.schemas import ArtifactRef
from athena.storage.artifact_store import ArtifactStore
from athena.workflows.search.idea_schemas import (
    FalsifiabilityReport,
    GapCandidate,
    GateDecision,
    HypothesisPackage,
    NoveltyEvidenceReport,
    PipelineCandidateResult,
    ResearchProblemInput,
    RevisionDraft,
    RevisionRound,
    SkepticReport,
    StructuralCheckReport,
    ValidationPlan,
)
from athena.research.ranking import RankedCandidate


# ====== 运行时依赖（不进 checkpoint） ======

@dataclass(frozen=True)
class PipelineDeps:
    """一次 Run 的运行时依赖，经 langgraph context_schema 注入，节点内用 get_runtime 取。

    两个信号量留在这里而不换成 langgraph 的 max_concurrency：后者是单一值，而本流水线需要
    两级不同限流（LLM 16 / retrieval 4，成本特性差一个量级），共用名额池会让慢的检索循环占满
    名额、把快的单轮调用堵在后面。

    Example:
        >>> deps = PipelineDeps(artifacts=store, corpus_ref=ref, llm_sem=s1, retrieval_sem=s2,
        ...     gap_miner_agent=a1, novelty_agent=a2, domain_review_agent=a3, model=None)  # doctest: +SKIP
    """
    artifacts: ArtifactStore
    corpus_ref: ArtifactRef
    llm_sem: asyncio.Semaphore
    retrieval_sem: asyncio.Semaphore
    gap_miner_agent: Agent
    novelty_agent: Agent
    domain_review_agent: Agent
    model: Model | str | None = None
    emit: Callable[..., Awaitable[None]] | None = None
    """事件回调，由 run_graph 用 dataclasses.replace 按需注入，不是调用方直接构造 PipelineDeps
    时要填的字段。candidate_node 靠它决定候选子图是走 ainvoke（emit 为 None，行为不变）还是
    astream（emit 非 None，逐节点发候选级事件）——放进 Deps 而不是单独当参数往下传，是因为
    候选子图内部节点（screen/novelty/.../regate）都只接收 CandidateState，只有 context_schema
    这条路能把它带到子图里。"""


# ====== 可序列化状态（进 checkpoint） ======

class CandidateState(TypedDict, total=False):
    """单个候选分支的状态。

    index 是候选在 candidates 里的下标，**保序的唯一依据**：Send fan-out 的分支完成顺序不受控，
    collect 节点按它排序后再喂 Elo。重构前这一层保序是 asyncio.gather 白送的，换成 Send 之后
    白送的部分没有了（设计 §4.4）。problem 由 fan_out_candidates 随分支带走——候选子图内部
    （如 validation_node 里的 match_verifier）需要它，但候选分支只收到 CandidateState，收不到
    顶层 PipelineState。

    review_reports 与 reviews 的两字段拆分（Task 3 引入）是 results/ordered_results 那对拆分
    在候选子图内部的翻版，原因相同：三个审阅视角节点（review_methodology/statistics/
    domain_consistency）从 novelty 并行展开，并发写同一个 key 必须挂 operator.add reducer，
    否则 langgraph 直接报错"Can receive only one value per step"（本设计文档口口相传前先
    实测过）。但 reviews 后续会被 rereview_node/refresh_node 整段覆盖（run_debate/
    refresh_stale_evidence 产出全新的一整份 3 视角报告，语义是替换不是追加）——如果 reviews
    本身也挂 operator.add，这次整段覆盖会变成追加，len(reviews) 从 3 变 6，hard_gate 的
    "每个视角恰好一份"强校验会当场炸掉。所以并发汇入用 review_reports（挂 reducer，只被三个
    审阅节点写），validation 节点（汇聚屏障）把它收拢成 reviews（不挂 reducer，普通覆盖字段，
    供 gate_node/revise_node/rereview_node 读写）。

    revisions 挂 operator.add（Task 4 引入）：revise_node 的 no-op 分支与 rereview_node 的两条
    分支都只产出"这一轮新增的那一条" RevisionRound，需要 reducer 把每轮的产出累加成完整轮次
    列表——这与 review_reports/results 是同一个模式，但没有那两处遇到的"覆盖 vs 追加"冲突：
    这里每次写入的都是全新的一条记录，不是重新处理过的旧数据写回去，不存在需要整段替换的场景。

    debated_perspective/prior_transcript/prior_summaries/revision_round_before/cleared/
    pending_draft/rereview_failed 七个字段只在候选子图内部的辩论循环（revise/rereview/
    refresh/regate 四个节点）之间顺序传递，均不挂 reducer——循环内每轮只有一个节点在跑，不
    存在并发写冲突。debated_perspective/prior_transcript 只在第一次进 revise_node 时计算并
    缓存（对照 run_debate：select_debate_opponent 在循环外只调一次，之后每轮复用同一个对手，
    不随 reviews 更新重选）。pending_draft 是 revise_node 真实修订（非 no-op）时产出的
    RevisionDraft，供紧随其后的 rereview_node 使用，在 reviser 失败/no-op 分支会被显式清成
    None，防止 route_after_revise 拿上一轮的旧草稿误路由。revision_round_before 只在候选
    分支第一次进 revise_node 时缓存一次（同样不随每轮重算），供 route_after_revise 判断这条
    候选有没有*曾经*被真的修订过——这与"这一轮有没有产出新草稿"（靠 pending_draft 判断）是
    两个独立的问题，答案可能不同（比如第 2 轮 no-op，但第 1 轮确实推进过）。cleared 是
    rereview_node 对本轮的清除判定；rereview_failed 是对手重表态失败时置位的信号，供
    route_after_rereview 与 cleared=False 区分开——前者必须立即结束循环（对齐 run_debate 的
    try/except: break），后者在轮次未满时还要继续下一轮。

    Example:
        >>> CandidateState(index=0, package=pkg, problem=problem)  # doctest: +SKIP
    """
    index: int
    package: HypothesisPackage
    problem: ResearchProblemInput
    structural: StructuralCheckReport
    falsifiability: FalsifiabilityReport
    novelty: NoveltyEvidenceReport | None
    review_reports: Annotated[list[SkepticReport], operator.add]
    reviews: list[SkepticReport]
    validation_plan: ValidationPlan | None
    decision: GateDecision
    revisions: Annotated[list[RevisionRound], operator.add]
    revision_blocking_factor: str | None
    debated_perspective: str
    pending_draft: RevisionDraft | None
    prior_transcript: str
    prior_summaries: list[str]
    revision_round_before: int
    cleared: bool
    rereview_failed: bool


class PipelineState(TypedDict, total=False):
    """顶层图状态。

    results 用 operator.add 作 reducer 累积各候选分支的产出，元素是 (index, result) 元组而不是
    裸 PipelineCandidateResult——PipelineCandidateResult 是既有领域模型，不应该为了图编排的保序
    需求加一个 branch_index 字段。ordered_results 是没有 reducer 的普通覆盖字段，由 collect
    节点按元组第一位（对应 CandidateState.index）重排后写入一次；不能让 collect 把排好序的
    结果写回 results 本身——那样会经过同一个 operator.add reducer 再加一遍，造成结果翻倍。

    Example:
        >>> PipelineState(problem=problem, gaps=[], candidates=[])  # doctest: +SKIP
    """
    problem: ResearchProblemInput
    gaps: list[GapCandidate]
    candidates: list[HypothesisPackage]
    sample_size: int
    results: Annotated[list[tuple[int, PipelineCandidateResult]], operator.add]
    ordered_results: list[PipelineCandidateResult]
    ranking: list[RankedCandidate]
