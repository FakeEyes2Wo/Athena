"""ValidationService — VALIDATE 阶段（supervisor_design §2.5 / Task 8）。

首版实现 final-test 的泛化差距计算。generalization gap 只在 trusted evaluator
提交 final_test_score 后计算，warning 只表示 final_test 比 test 差，不触发
第二名评估或 execution 失败。
"""

import math

from athena.core.contracts import new_id
from athena.research.contracts import ValidationResult

# SOTA/泛化 tie tolerance（design §2.5 "EvalSpec 已有的 math.isclose tolerance"）。
_TIE_REL_TOL = 1e-9
_TIE_ABS_TOL = 1e-12


def generalization_gap(
    test_score: float,
    final_test_score: float,
    direction: str,
) -> float:
    """计算 test 与 final-test 的泛化差距（正值表示 final_test 更差）。

    maximize 指标 gap = ``test - final``；minimize 指标 gap = ``final - test``
    （design §2.5）。返回裸差值，warning 判断由调用方按 tolerance 决定。
    """
    if direction == "minimize":
        return final_test_score - test_score
    return test_score - final_test_score


def generalization_warning(
    gap: float,
    *,
    rel_tol: float = _TIE_REL_TOL,
    abs_tol: float = _TIE_ABS_TOL,
) -> bool:
    """gap 为正且超过 EvalSpec 数值 tie tolerance 时 warning=true。

    纯数值噪声（如 0.8 与 0.8+1e-13）不算泛化退化，不触发 warning。
    """
    return gap > 0 and not math.isclose(gap, 0.0, rel_tol=rel_tol, abs_tol=abs_tol)


class ValidationService:
    """VALIDATE 的确定性编排服务（final-test 结果合同）。"""

    def build_result(
        self,
        *,
        test_score: float,
        final_test_score: float,
        direction: str,
    ) -> ValidationResult:
        """在 final_test_score 已提交后构建 VALIDATE 最终结论。

        按 direction 计算 gap；gap 为正且超过 EvalSpec tie tolerance 时
        warning=true，但状态仍 COMPLETED（真实但不理想的泛化表现不是系统
        执行失败，design §2.5）。
        """
        gap = generalization_gap(test_score, final_test_score, direction)
        return ValidationResult(
            result_id=new_id("vr"),
            status="COMPLETED",
            test_score=test_score,
            final_test_score=final_test_score,
            generalization_gap=gap,
            generalization_warning=generalization_warning(gap),
        )
