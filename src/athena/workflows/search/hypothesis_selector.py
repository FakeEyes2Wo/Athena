"""从 run_full_pipeline 的产出里选出下一批要验证的假设（步骤 [10]，Hypothesis Selector）。

排序权全部在 research/ranking.py 的纯算法 Elo（HypoPriList/RankedCandidate）；本模块不
引入 LLM 交互，不做任何可行性/新颖性/优先级判断——只是把 run_full_pipeline 已经排好序的
结果，按 idea_id 交叉引用出完整审计记录，再按资源预算截断，呼应主设计里 Co-Scientist
"排序纯算法化"的思路。

已知限制（诚实记录，不假装接好了）：Hypothesis Selector 原 stub 的设计意图提到"已执行
实验"也是选择依据之一，但 Idea Generation 目前完全不知道任何候选有没有真的跑过实验——
ResearchTree 挂接（把候选写进研究树）是一个更大的架构决策，`ResearchTree`/`Experiment`
的数据模型本身还没有"待验证候选"这种状态可以承载，这一轮不改（见
docs/idea-generation-remaining-work.md 任务二）。本函数因此假设传入的 results/ranking
本身不包含已经跑过实验的候选——多轮调用之间"这个候选选过了"的去重是调用方的责任，不是
这个函数的责任。
"""

from athena.research.ranking import RankedCandidate
from athena.workflows.search.idea_schemas import PipelineCandidateResult


def select_next_hypotheses(
    results: list[PipelineCandidateResult],
    ranking: list[RankedCandidate],
    *,
    budget: int = 1,
) -> list[PipelineCandidateResult]:
    """按 Elo 评分从高到低选出最多 budget 个候选的完整审计记录。

    ranking 已经只包含存活候选（rank_node 只把 PASS/EXPLORATORY 的候选注册进
    HypoPriList），本函数不重复这条判定，纯粹是"按已经算好的排名截断 + 交叉引用"，
    不引入新的评判逻辑——判定权仍然只在 gatekeeper。

    budget 就是当前唯一接入的资源预算形式：最多选几个候选去做下一步验证。没有更细的
    预算模型（比如按 token/计算成本折算）可以复用，不在没有实测依据的情况下发明一个。

    budget <= 0 返回空列表。ranking 引用了 results 里不存在的 idea_id 视为调用方传入了
    不匹配的一对 results/ranking（两者必须来自同一次 run_full_pipeline 调用），直接
    抛 KeyError，不静默跳过——静默跳过会把一个真实的调用错误伪装成"候选选少了"。

    Example:
        >>> select_next_hypotheses([], [], budget=1)
        []
    """
    if budget <= 0:
        return []

    by_idea_id = {result.package.idea_id: result for result in results}
    ordered = sorted(ranking, key=lambda candidate: candidate.rating, reverse=True)

    selected: list[PipelineCandidateResult] = []
    for ranked in ordered[:budget]:
        if ranked.idea_id not in by_idea_id:
            raise KeyError(
                f"ranking references idea_id {ranked.idea_id!r} not present in results — "
                "results and ranking must come from the same run_full_pipeline call"
            )
        selected.append(by_idea_id[ranked.idea_id])
    return selected
