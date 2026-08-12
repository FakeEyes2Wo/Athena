"""SearchService — SEARCH 图注册、ranking 与唯一 SOTA（supervisor_design §2.5）。

所有通过结构校验的 Hypothesis 在排名前先写入 ResearchGraph（包括本轮最终未
选中的候选）；未选中只形成 deferred ranking，不把科学状态改成 REFUTED/REJECTED。
唯一 SOTA 严格按共同 test split 上的 primary ``test_score`` 与 EvalSpec 方向排序；
分数差落在 EvalSpec 纯数值 tie tolerance 内视为平局，平局保留当前 SOTA。
"""

import math
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from athena.core.contracts import ArtifactRef, CommitHash, new_id
from athena.core.research_models import (
    ComparisonVerdict,
    EvalResult,
    ExperimentPlan,
    Hypothesis,
)
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
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
        """当前 ResearchTree（假设/实验/SOTA 图）。"""
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

    async def prepare_experiment(
        self,
        *,
        hypotheses: list[Hypothesis],
        make_worktree: Callable[[str, str], GitWorkBranch],
        run_config_ref: ArtifactRef,
        parent_sota_id: str | None = None,
    ) -> dict[str, object]:
        """注册假设 → 选一个 pending → 建 worktree → RUNNING 实验，返回 Code 赋值。

        一个 SEARCH_ROUND 只执行一个假设；其余保持 pending（design §Minimal-Patch）。
        """
        for hypothesis in hypotheses:
            self._tree.add_hypothesis(hypothesis)
        pending = self._tree.pending_hypotheses()
        if not pending:
            raise ValueError("no pending hypothesis to run")
        selected = pending[0]
        if not selected.id:
            raise ValueError("selected hypothesis has no id")
        experiment_id = new_id("exp")
        plan = ExperimentPlan(
            kind="search",
            change=selected.intervention,
            rubrics=[selected.statement],
            run_config_ref=run_config_ref,
            budget={},
            acceptance_rule="improves over parent SOTA",
        )
        gitwork = make_worktree(experiment_id, f"athena/search/{experiment_id}")
        if inspect.isawaitable(gitwork):
            gitwork = await gitwork
        self._tree.add_experiment(
            experiment_id,
            Experiment(
                hypothesis_id=selected.id,
                commit=gitwork.base_commit,
                plan=plan,
                gitwork=gitwork,
                status=ExperimentStatus.PENDING,
                parent_id=parent_sota_id or self._tree.best_experiment_id(),
            ),
        )
        self._tree.transition_experiment(experiment_id, ExperimentStatus.RUNNING)
        return {
            "experiment_id": experiment_id,
            "workspace": gitwork.path,
            "environment_root": gitwork.path,
            "hypothesis": selected.model_dump(mode="json"),
            "parent_sota": parent_sota_id,
        }

    async def finish_experiment(
        self,
        *,
        experiment_id: str,
        predictions: str,
        predictions_ref: ArtifactRef,
        evaluator: Any,
        eval_bundle: Any,
        direction: Literal["maximize", "minimize"],
        commit: CommitHash | None = None,
    ) -> dict[str, object]:
        """评分 → complete → 与父 SOTA 比较 → set_sota；未知实验 ID 抛 KeyError。"""
        experiment = self._tree.get_experiment(experiment_id)
        if experiment.status is not ExperimentStatus.RUNNING:
            raise ValueError(f"experiment {experiment_id} is not RUNNING")
        evaluation = await evaluator.score(
            eval_bundle=eval_bundle,
            predictions=predictions,
            candidate_id=experiment_id,
            direction=direction,
        )
        parent_score = self._parent_score(experiment.parent_id)
        if math.isnan(parent_score):
            winner: Literal["current", "candidate"] = "candidate"  # 首个实验即 SOTA
        else:
            winner = self.compare(
                parent_score, evaluation.test_score, direction=direction
            )
        self._tree.complete_experiment(
            experiment_id,
            eval=EvalResult(
                experiment_id=experiment_id,
                primary=evaluation.test_score,
                per_sample=predictions_ref,
            ),
            verdict=ComparisonVerdict(
                winner="candidate" if winner == "candidate" else "baseline",
                p_value=0.0,
            ),
            artifacts={"predictions": predictions_ref},
            commit=commit,
        )
        if winner == "candidate":
            self._tree.set_sota(experiment_id)
        return {
            "graph": self._tree,
            "test_score": evaluation.test_score,
            "sota_experiment_id": self._tree.best_experiment_id(),
        }

    def _parent_score(self, parent_id: str | None) -> float:
        """父实验的 eval.primary；无父或父无分数返回 NaN（首个候选即 SOTA）。"""
        if parent_id is None:
            return float("nan")
        try:
            parent = self._tree.get_experiment(parent_id)
        except KeyError:
            return float("nan")
        return parent.eval.primary if parent.eval is not None else float("nan")

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
