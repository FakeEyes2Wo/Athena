"""PaperScout 的检索状态、动作记录与结果模型。

PaperScout 把论文检索建模成 POMDP：隐状态是累积的 paper pool，Agent 每一步只看到
pool 的一个摘要视图（observation），并选择 ``search`` 或 ``expand`` 动作。这里的模型
对应论文里的三个对象——池中的一篇论文、一次动作的结果、一次运行的统计。

相关性分数 ``relevance`` 是 [0,1] 的连续值，既决定论文能否进池（τ），也决定最终交付
集合（ρ ≥ 0.5）和排序，因此它是唯一贯穿全流程的排序信号。
"""

from typing import Literal

from pydantic import BaseModel, Field

from athena.core.schemas import ArtifactRef
from athena.research.paper_source.schemas import PaperSourcePolicy

ACCEPT_THRESHOLD = 0.01
RETAIN_THRESHOLD = 0.5
OBSERVATION_EXPANDED = 10
OBSERVATION_UNEXPANDED = 10
MAX_ABSTRACT_WORDS = 400
IDLE_TURNS_BEFORE_STOP = 3
REWARD_TOP_K = 3
REWARD_THRESHOLD = 0.4
REPEAT_PENALTY = 0.5
SEARCH_COST = 0.1
EXPAND_COST = 0.05


class ScoutPaper(BaseModel):
    """池中的一篇论文，带有它是怎样被发现的以及它的相关性分数。"""

    paper_key: str = Field(description="Stable identity key used for pool dedup.")
    arxiv_id: str = Field(default="", description="Bare arXiv id without version.")
    doi: str = Field(default="", description="Normalized DOI when known.")
    s2_paper_id: str = Field(default="", description="Semantic Scholar paper id.")
    title: str = Field(description="Paper title.")
    abstract: str = Field(default="", description="Abstract text; may be empty.")
    year: int | None = Field(default=None, description="Publication year.")
    published_date: str = Field(default="", description="ISO date when known.")
    citation_count: int | None = Field(default=None, description="Citations if known.")
    source: Literal["search", "expand"] = Field(
        description="Which action first surfaced this paper."
    )
    origin: str = Field(
        default="",
        description="Query text for search results, parent title for expansions.",
    )
    channel: str = Field(default="", description="Backend that returned the paper.")
    relevance: float = Field(default=0.0, ge=0.0, le=1.0, description="Score in [0,1].")
    expanded: bool = Field(
        default=False, description="Whether its references were already followed."
    )


class ScoutAction(BaseModel):
    """一次 ``search`` 或 ``expand`` 的执行结果。

    ``reward`` 复现论文的过程奖励（top-k 相关性增益减去调用成本，重复动作为负），
    在线推理不使用它，只作为可审计的过程信号记录下来。
    """

    step: int = Field(ge=1, description="1-based agent step that issued the action.")
    kind: Literal["search", "expand"] = Field(description="Action type.")
    argument: str = Field(description="Query text or arXiv id.")
    returned: int = Field(default=0, ge=0, description="Raw results from the backend.")
    accepted: int = Field(
        default=0, ge=0, description="Papers newly added to the pool."
    )
    reward: float = Field(default=0.0, description="Process reward for this action.")
    repeated: bool = Field(default=False, description="Action had already been taken.")
    error: str = Field(default="", description="Backend failure, empty when fine.")


class ScoutStats(BaseModel):
    """一次运行的成本与停止原因摘要。"""

    steps: int = Field(default=0, ge=0)
    search_actions: int = Field(default=0, ge=0)
    expand_actions: int = Field(default=0, ge=0)
    repeated_actions: int = Field(default=0, ge=0)
    pool_size: int = Field(default=0, ge=0)
    scored_papers: int = Field(default=0, ge=0)
    retained_papers: int = Field(default=0, ge=0)
    policy_calls: int = Field(default=0, ge=0)
    scorer_calls: int = Field(default=0, ge=0)
    backend_requests: int = Field(default=0, ge=0)
    wall_seconds: float = Field(default=0.0, ge=0.0)
    stop_reason: str = Field(default="", description="Why the loop terminated.")
    errors: list[str] = Field(default_factory=list)


class ScoutRequest(BaseModel):
    """一次 PaperScout 运行的输入。

    ``published_to`` 对应基准构造时的发布日期上限；``max_papers`` 只截断交付集合，
    不改变检索过程。
    """

    schema_version: Literal["1.0"] = "1.0"
    query: str = Field(min_length=1, description="Natural language paper search need.")
    published_to: str = Field(
        default="", description="Inclusive ISO upper bound on publication date."
    )
    max_steps: int = Field(default=10, ge=1, description="Hard cap on agent steps.")
    max_parallel_calls: int = Field(
        default=5, ge=1, description="Tool calls honoured per step."
    )
    search_top_k: int = Field(default=10, ge=1, description="Results per search call.")
    expand_top_k: int = Field(
        default=20, ge=1, description="References followed per expand call."
    )
    max_papers: int = Field(default=0, ge=0, description="0 means no handoff cap.")
    max_seconds: float = Field(default=600.0, gt=0, description="Wall-clock budget.")
    paper_source_policy: PaperSourcePolicy = Field(
        default_factory=PaperSourcePolicy,
        description="Fetch policy carried into the generated paper_source request.",
    )


class ScoutCorpus(BaseModel):
    """交付与审计入口：最终论文、完整池和逐动作轨迹。"""

    schema_version: Literal["1.0"] = "1.0"
    query: str = Field(description="The request this corpus answers.")
    retained: list[ScoutPaper] = Field(
        description="Papers with relevance >= RETAIN_THRESHOLD, ranked."
    )
    pool: list[ScoutPaper] = Field(description="Every paper accepted into the pool.")
    actions: list[ScoutAction] = Field(description="Ordered action trace.")
    stats_ref: ArtifactRef = Field(description="Reference to the run statistics.")


class PaperScoutResult(BaseModel):
    """Agent Turn 的顶层结果。"""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["complete", "partial"] = Field(
        description="partial when a backend failed or the budget cut the run short."
    )
    corpus_ref: ArtifactRef = Field(description="Reference to the ScoutCorpus.")
    stats_ref: ArtifactRef = Field(description="Reference to the ScoutStats.")
    paper_source_request_ref: ArtifactRef | None = Field(
        default=None,
        description=(
            "PaperSourceRequest ready for paper_fetch; None when no delivered paper "
            "carries an identifier paper_source can resolve."
        ),
    )
    paper_count: int = Field(default=0, ge=0, description="Retained paper count.")
    warnings: list[str] = Field(default_factory=list)
