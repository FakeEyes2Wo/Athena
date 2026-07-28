"""AcademicSurvey 的版本化在线预算。"""

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SurveyBudget:
    """一次 SPAR 检索允许消耗的确定性上限。"""

    version: str
    max_rounds: int
    max_initial_queries: int
    max_candidates: int
    max_judgments: int
    max_refchain_seeds: int
    references_per_seed: int
    max_final_papers: int
    max_seconds: int
    search_batch_size: int = 20
    judgment_concurrency: int = 8
    max_batches_per_round: int = 8
    ucb_exploration: float = 1.4
    query_overlap_threshold: float = 0.8


_BUDGETS = {
    "fast": SurveyBudget(
        "academic-survey-budget-v2",
        2,
        4,
        250,
        120,
        5,
        10,
        20,
        180,
        max_batches_per_round=4,
    ),
    "diligent": SurveyBudget(
        "academic-survey-budget-v2",
        4,
        8,
        1000,
        500,
        20,
        20,
        50,
        600,
        max_batches_per_round=8,
    ),
}


def budget_for(mode: Literal["fast", "diligent"]) -> SurveyBudget:
    """返回不可由模型修改的 mode 预算。"""
    return _BUDGETS[mode]


def ucb_score(
    channel: str,
    pulls: dict[str, int],
    rewards: dict[str, int],
    exploration: float,
) -> float:
    """按首次发现的 relevant 数和 search batch 数计算 UCB。"""
    channel_pulls = pulls.get(channel, 0)
    if channel_pulls == 0:
        return math.inf
    total = max(1, sum(pulls.values()))
    mean_reward = rewards.get(channel, 0) / channel_pulls
    return mean_reward + exploration * math.sqrt(math.log(total) / channel_pulls)


def allocate_search_indices(
    arms: list[tuple[str, str, str]],
    query_pulls: dict[str, int],
    arm_pulls: dict[str, int],
    channel_pulls: dict[str, int],
    channel_rewards: dict[str, int],
    budget: SurveyBudget,
) -> list[int]:
    """先覆盖 query family/channel，再按 UCB 选择稳定的检索 arm。"""
    remaining = list(range(len(arms)))
    selected: list[int] = []
    local_queries = dict(query_pulls)
    local_arms = dict(arm_pulls)
    local_channels = dict(channel_pulls)
    cap = min(len(arms), budget.max_batches_per_round)
    while remaining and len(selected) < cap:
        index = min(
            remaining,
            key=lambda value: (
                local_queries.get(arms[value][0], 0) > 0,
                local_channels.get(arms[value][1], 0) > 0,
                local_arms.get(arms[value][2], 0) > 0,
                -ucb_score(
                    arms[value][1],
                    local_channels,
                    channel_rewards,
                    budget.ucb_exploration,
                ),
                value,
            ),
        )
        selected.append(index)
        remaining.remove(index)
        query_id, channel, arm_key = arms[index]
        local_queries[query_id] = local_queries.get(query_id, 0) + 1
        local_channels[channel] = local_channels.get(channel, 0) + 1
        local_arms[arm_key] = local_arms.get(arm_key, 0) + 1
    return selected
