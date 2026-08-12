"""EvaluationPolicy — 可信评估模式选择（supervisor_design §2.5）。

默认对 classical ML 与 deep learning 都用 train 内 ``k=5``：记录 fold 分数、
mean、std，随后完整 train 再训练一次并在 test 上评估一次。test_score 决定
排行榜与唯一 SOTA；K-fold 只提供稳定性证据。成本预检过高或 K-fold disabled
时，整轮在启动前确定性切换为 single-test（不在运行中按结果临时降级）。
"""

from typing import Literal

from pydantic import BaseModel, Field

from athena.research.contracts import (
    CandidateEvaluation,
    DataScriptBundle,
    ExecutionConfig,
)
from athena.research.script_runner import DataScriptRunner

EvaluationMode = Literal["kfold-5", "single-test"]

_COST_PREVIEW_RATIO = 0.4
_CANDIDATE_OVERHEAD = 1.5


class CostSnapshot(BaseModel):
    """成本预检输入：估算训练时长与剩余时长。"""

    baseline_full_train_duration: float = Field(default=1.0, ge=0)
    remaining_duration_seconds: float = Field(default=3600.0, ge=0)
    selected_candidates: int = Field(default=2, ge=1)
    k_folds: int = Field(default=5, ge=1)
    single_fold_timeout_seconds: float | None = None


class EvaluationPolicy:
    """按 ExecutionConfig 与成本快照确定性选择评估模式。"""

    def choose_mode(
        self, config: ExecutionConfig, cost: CostSnapshot
    ) -> EvaluationMode:
        """选择整轮统一的评估模式；同一轮候选不得分别降级。"""
        if config.kfold_policy == "disabled":
            return "single-test"
        if config.kfold_policy == "required":
            return "kfold-5"
        # auto：成本预检（design §2.5 公式）
        # TODO(search-bootstrap): test predictions 重采样成本完成基准评估且 EvalSpec 获得 bootstrap seed/count/CI/min_effect 合同后，增加 paired bootstrap；首版使用 5-fold 或单次 test。
        if cost.remaining_duration_seconds <= 0:
            return "single-test"
        estimated = (
            cost.baseline_full_train_duration
            * (cost.k_folds + 1)
            * cost.selected_candidates
            * _CANDIDATE_OVERHEAD
        )
        if estimated > cost.remaining_duration_seconds * _COST_PREVIEW_RATIO:
            return "single-test"
        if (
            cost.single_fold_timeout_seconds is not None
            and cost.baseline_full_train_duration > cost.single_fold_timeout_seconds
        ):
            return "single-test"
        return "kfold-5"


class TrustedEvaluator:
    """唯一可信 test/final-test evaluator：运行冻结 eval bundle 对齐预测与标签。

    只有 trusted evaluator 读取 test/final labels；脚本异常、对齐失败或缺少
    ``primary`` 分数都视为该 evaluation 失败，不允许平台侧补救或重新评分。
    """

    def __init__(self, runner: DataScriptRunner) -> None:
        self._runner = runner

    async def score(
        self,
        *,
        eval_bundle: DataScriptBundle,
        predictions: str,
        candidate_id: str,
        direction: Literal["maximize", "minimize"],
    ) -> CandidateEvaluation:
        """运行 eval 入口，只注入 predictions；labels 来自冻结 bundle（design 修复 5）。"""
        result = await self._runner.run(
            eval_bundle,
            request={"predictions": predictions},
            extra_files={"predictions.csv": predictions},
            output_schema={"primary": None},
        )
        primary = result.outputs.get("primary")
        if primary is None:
            raise ValueError("candidate evaluation produced no primary score")
        return CandidateEvaluation(
            candidate_id=candidate_id,
            test_score=float(primary),
            direction=direction,
        )
