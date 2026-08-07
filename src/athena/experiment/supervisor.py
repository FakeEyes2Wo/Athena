"""状态转换、预算、终止。搜索循环的唯一决策者。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from athena.evaluation.types import ComparisonVerdict

if TYPE_CHECKING:
    from athena.research.budget import BudgetSnapshot


# NOTE(supervisor-policy): The current policy intentionally covers only the
# verdict, remaining-budget, and no-improvement rules.


@dataclass
class Decision:
    """包含动作类型和可读原因的 Supervisor 决策。"""

    action: str  # "ACCEPT" | "REJECT" | "STOP"
    reason: str


class Supervisor:
    """管理状态转换、预算和终止。唯一决策者。"""

    def decide(self, verdict: ComparisonVerdict, budget: "BudgetSnapshot") -> Decision:
        """根据比较裁决和预算状态决策 ACCEPT/REJECT/STOP。"""
        if budget.is_exhausted:
            return Decision("STOP", "Budget exhausted before this decision")

        if verdict.winner == "candidate":
            if budget.remaining <= 1:
                return Decision("ACCEPT", "Final improvement -- budget ending")
            return Decision("ACCEPT", "Candidate improves over baseline")

        if verdict.winner == "baseline":
            if budget.no_improve_streak + 1 >= budget.max_no_improve:
                return Decision(
                    "STOP",
                    f"No improvement for {budget.max_no_improve} consecutive experiments",
                )
            return Decision("REJECT", "No improvement -- continue search")

        # 平局
        if budget.no_improve_streak + 1 >= budget.max_no_improve:
            return Decision("STOP", f"Tie streak exhausted budget")
        return Decision("REJECT", "Tie -- no improvement")
