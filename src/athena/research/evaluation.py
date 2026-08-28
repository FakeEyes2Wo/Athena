"""TrustedEvaluator — 唯一可信 test/final-test evaluator（supervisor_design §2.5）。

只有 trusted evaluator 读取 test/final labels；脚本异常、对齐失败或缺少
``primary`` 分数都视为该 evaluation 失败，不允许平台侧补救或重新评分。
"""

import math
import subprocess
from typing import Literal

from athena.research.contracts import CandidateEvaluation, DataScriptBundle
from athena.research.script_runner import DataScriptRunner


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
        predictions: dict[str, bytes],
        candidate_id: str,
        direction: Literal["maximize", "minimize"],
        predictions_root: str,
    ) -> CandidateEvaluation:
        """运行 eval 入口，只注入 predictions 目录；labels 来自冻结 bundle（design 修复 5）。

        评估脚本崩溃/超时/缺字段/非有限分数，都视为该候选的评分失败（ValueError），
        由上层按 ``scoring_failed`` 重试，而不是误判成 evaluator 基础设施故障而终止。
        ``predictions`` 是预测目录的内存表示（相对路径 → 字节），``predictions_root``
        是注入到重建 bundle 工作目录的目录根（如 "predictions"）；runner 据此把每个
        文件写为 ``{predictions_root}/{相对路径}``，供 trusted evaluator 在对齐后运行
        eval 入口读取。
        """
        try:
            extra_files = {
                f"{predictions_root}/{rel}": content
                for rel, content in predictions.items()
            }
            result = await self._runner.run(
                eval_bundle,
                request={},
                extra_files=extra_files,
                output_schema={"primary": None},
            )
        except (subprocess.SubprocessError, RuntimeError) as exc:
            raise ValueError(f"evaluator failed to produce a score: {exc}") from exc
        primary = result.outputs.get("primary")
        if primary is None:
            raise ValueError("candidate evaluation produced no primary score")
        if isinstance(primary, bool) or not isinstance(primary, (int, float, str)):
            raise ValueError(
                f"primary score must be a number, got {type(primary).__name__}"
            )
        try:
            metric = float(primary)
        except (TypeError, ValueError):
            raise ValueError(
                f"primary score must be numeric, got {primary!r}"
            ) from None
        if not math.isfinite(metric):
            raise ValueError(f"primary score must be finite, got {primary!r}")
        test_se = result.outputs.get("test_se")
        test_n = result.outputs.get("test_n")
        if test_se is not None:
            try:
                test_se = float(test_se)
            except (TypeError, ValueError):
                raise ValueError(
                    f"test_se must be numeric, got {test_se!r}"
                ) from None
            if not math.isfinite(test_se) or test_se < 0:
                raise ValueError(f"test_se must be a non-negative finite number, got {test_se!r}")
        if test_n is not None:
            try:
                test_n = int(test_n)
            except (TypeError, ValueError):
                raise ValueError(
                    f"test_n must be an integer, got {test_n!r}"
                ) from None
            if test_n < 0:
                raise ValueError(f"test_n must be non-negative, got {test_n!r}")
        return CandidateEvaluation(
            candidate_id=candidate_id,
            test_score=metric,
            test_se=test_se,
            test_n=test_n,
            direction=direction,
        )
