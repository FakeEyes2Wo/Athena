"""ValidationService — VALIDATE 阶段（supervisor_design §2.5 / Task 8）。

首版实现 ablation 的依赖闭包与 final-test 的泛化差距计算。FULL_LINEAGE 对
每个 accepted intervention 做 leave-one-out 前，先计算其依赖闭包
（intervention 自身 + 全部传递依赖者），确定移除该 intervention 会波及哪些
下游组件。generalization gap 只在 trusted evaluator 提交 final_test_score 后
计算，warning 只表示 final_test 比 test 差，不触发第二名评估或 execution 失败。
reproduce_sota 编排随 Task 8 后续实现。
"""

import math
from collections.abc import Mapping, Sequence

from athena.core.contracts import new_id
from athena.research.contracts import ValidationResult

# 与 search.py 的 SOTA tie tolerance 一致（design §2.5 "EvalSpec 已有的 math.isclose tolerance"）。
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

    与 search.py 的 SOTA 平局判定使用同一 ``math.isclose`` tolerance：纯数值
    噪声（如 0.8 与 0.8+1e-13）不算泛化退化，不触发 warning。
    """
    return gap > 0 and not math.isclose(gap, 0.0, rel_tol=rel_tol, abs_tol=abs_tol)


def dependency_closure(
    graph: Mapping[str, Sequence[str]], intervention_id: str
) -> tuple[str, ...]:
    """返回 intervention 的依赖闭包（BFS，稳定顺序，含自身）。

    ``graph[node]`` 为该节点直接依赖者列表（DAG）。从 ``intervention_id``
    BFS 遍历：先自身，再按声明顺序逐层展开传递依赖者。用于 leave-one-out
    消融——移除该 intervention 必须连带重跑闭包内全部下游。
    """
    ordered: list[str] = []
    seen: set[str] = set()
    frontier = [intervention_id]
    while frontier:
        node = frontier.pop(0)
        if node in seen:
            continue
        seen.add(node)
        ordered.append(node)
        frontier.extend(graph.get(node, ()))
    return tuple(ordered)


class ValidationService:
    """VALIDATE 的确定性编排服务（ablation scope / final-test 结果合同）。"""

    def ablation_scope(
        self,
        graph: Mapping[str, Sequence[str]],
        accepted_interventions: Sequence[str],
    ) -> dict[str, tuple[str, ...]]:
        """FULL_LINEAGE：每个 accepted intervention 的依赖闭包。

        返回 ``{intervention_id: closure}``；闭包至少包含自身，供 leave-one-out
        决定每次移除的波及范围。
        """
        return {
            intervention: dependency_closure(graph, intervention)
            for intervention in accepted_interventions
        }

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
