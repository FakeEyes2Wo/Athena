"""SearchService — SEARCH 图注册、ranking 与唯一 SOTA（supervisor_design §2.5）。

所有通过结构校验的 Hypothesis 在排名前先写入 ResearchGraph（包括本轮最终未
选中的候选）；未选中只形成 deferred ranking，不把科学状态改成 REFUTED/REJECTED。
唯一 SOTA 严格按共同 test split 上的 primary ``test_score`` 与 EvalSpec 方向排序；
分数差落在 EvalSpec 纯数值 tie tolerance 内视为平局，平局保留当前 SOTA。
"""

import math
from dataclasses import dataclass
from typing import Literal

from athena.core.contracts import new_id
from athena.core.research_models import Hypothesis
from athena.core.research_tree import ResearchTree
from athena.research.contracts import CandidateEvaluation, RankingRound

_TIE_REL_TOL = 1e-9
_TIE_ABS_TOL = 1e-12


@dataclass
class RankingRoundResult:
    """freeze_round 的返回值：graph、append-only RankingRound 与 SOTA 判定。"""

    graph: ResearchTree
    ranking: RankingRound
    sota: CandidateEvaluation | None  # None = 保留当前 SOTA
    winner: Literal["current", "candidate"]


class SearchService:
    """确定性 SEARCH 服务：图入库、按 test_score 排序、唯一 SOTA 事务输入。"""

    def __init__(self, tree: ResearchTree | None = None) -> None:
        self._tree = tree or ResearchTree()

    @property
    def graph(self) -> ResearchTree:
        return self._tree

    def register_hypotheses(self, hypotheses: list[Hypothesis]) -> ResearchTree:
        """所有结构有效假设先写入图（pending），排名前不得丢弃。"""
        for hypothesis in hypotheses:
            self._tree.add_hypothesis(hypothesis)
        return self._tree

    def compare(
        self,
        current: float,
        candidate: float,
        *,
        direction: Literal["maximize", "minimize"] = "maximize",
    ) -> Literal["current", "candidate"]:
        """EvalSpec 方向比较；tie 保留当前 SOTA（design §2.5）。"""
        # TODO(search-cost-tiebreak): 成本指标合同获批后，可在 test score 平局时比较训练时间、推理时间和内存；首版平局始终保留当前 SOTA。
        if math.isclose(candidate, current, rel_tol=_TIE_REL_TOL, abs_tol=_TIE_ABS_TOL):
            return "current"
        if direction == "maximize":
            return "candidate" if candidate > current else "current"
        return "candidate" if candidate < current else "current"

    def freeze_round(
        self,
        parent_score: float,
        candidates: list[CandidateEvaluation],
        *,
        selected_count: int = 2,
        parent_sota_id: str | None = None,
    ) -> RankingRoundResult:
        """冻结一轮 RankingRound：排序、选择、唯一 SOTA 判定（append-only）。

        完成顺序不得改变结果：只与冻结父 SOTA 比较，最多产生一个新 SOTA。
        """
        if not candidates:
            raise ValueError("freeze_round requires at least one candidate")
        direction = candidates[0].direction
        ranked = sorted(
            candidates,
            key=lambda c: c.test_score,
            reverse=(direction == "maximize"),
        )
        selected = ranked[:selected_count]
        deferred = ranked[selected_count:]
        best = ranked[0]
        outcome = self.compare(parent_score, best.test_score, direction=direction)
        # TODO(search-pareto): 需要多目标 EvalSpec、frontier 上限/剪枝、预算分配和 VALIDATE 最终选择合同获批后，再扩展为 Pareto front；首版保持唯一 SOTA。
        winner = best if outcome == "candidate" else None
        ranking = RankingRound(
            round_id=new_id("round"),
            selected_ids=[c.candidate_id for c in selected],
            deferred_ids=[c.candidate_id for c in deferred],
            scores={c.candidate_id: c.test_score for c in candidates},
            sota_experiment_id=winner.candidate_id if winner else parent_sota_id,
        )
        return RankingRoundResult(
            graph=self._tree, ranking=ranking, sota=winner, winner=outcome
        )

    def register_and_select(
        self, hypotheses: list[Hypothesis], selected_count: int
    ) -> RankingRoundResult:
        """编排/测试辅助：先全部入图，再确定性选择前 ``selected_count`` 个。"""
        ids = [self._tree.add_hypothesis(h) for h in hypotheses]
        ranking = RankingRound(
            round_id=new_id("round"),
            selected_ids=ids[:selected_count],
            deferred_ids=ids[selected_count:],
            scores={},
            sota_experiment_id=None,
        )
        return RankingRoundResult(
            graph=self._tree, ranking=ranking, sota=None, winner="current"
        )
