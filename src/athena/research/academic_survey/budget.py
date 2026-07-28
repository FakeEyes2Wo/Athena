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
        "academic-survey-budget-v1",
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
        "academic-survey-budget-v1",
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


def allocate_batch_indices(
    channels: list[str],
    pulls: dict[str, int],
    rewards: dict[str, int],
    budget: SurveyBudget,
    round_index: int,
) -> list[int]:
    """首轮 probe 每个通道，后续按 UCB 稳定选择批次。"""
    cap = min(len(channels), budget.max_batches_per_round)
    if round_index == 0:
        selected = []
        seen = set()
        for index, channel in enumerate(channels):
            if channel not in seen:
                selected.append(index)
                seen.add(channel)
        selected.extend(
            index for index in range(len(channels)) if index not in selected
        )
        return selected[:cap]
    return sorted(
        range(len(channels)),
        key=lambda index: (
            -ucb_score(channels[index], pulls, rewards, budget.ucb_exploration),
            index,
        ),
    )[:cap]
