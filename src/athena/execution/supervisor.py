"""State transitions, budget, termination. Sole decision-maker for the SEARCH loop."""

from dataclasses import dataclass
from athena.core.schemas import ComparisonVerdict


@dataclass
class Decision:
    action: str  # "ACCEPT" | "REJECT" | "STOP"
    reason: str


class Supervisor:
    """Owns state transitions, budget, termination. Sole decision-maker."""

    def decide(self, verdict: ComparisonVerdict, budget: "BudgetSnapshot") -> Decision:
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

        # tie
        if budget.no_improve_streak + 1 >= budget.max_no_improve:
            return Decision("STOP", f"Tie streak exhausted budget")
        return Decision("REJECT", "Tie -- no improvement")
