from pydantic import BaseModel


class BudgetSnapshot(BaseModel):
    """Track remaining experiments and the consecutive no-improvement streak."""

    remaining: int = 20
    no_improve_streak: int = 0
    max_no_improve: int = 5
    is_exhausted: bool = False

    def consume(self, improved: bool) -> None:
        self.remaining -= 1
        self.no_improve_streak = 0 if improved else self.no_improve_streak + 1
        if self.remaining <= 0 or self.no_improve_streak >= self.max_no_improve:
            self.is_exhausted = True


class RunMode(BaseModel):
    """Configure human-in-the-loop and debug execution modes."""

    hil: bool = False
    debug: bool = False
