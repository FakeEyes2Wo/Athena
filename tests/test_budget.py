from athena.research.budget import BudgetSnapshot


def test_budget_exhausted_by_count():
    b = BudgetSnapshot(remaining=1, max_no_improve=5)
    b.consume(improved=False)
    assert b.is_exhausted


def test_budget_exhausted_by_streak():
    b = BudgetSnapshot(remaining=10, max_no_improve=3)
    b.consume(improved=False)
    b.consume(improved=False)
    assert not b.is_exhausted
    b.consume(improved=False)
    assert b.is_exhausted


def test_budget_improvement_resets_streak():
    b = BudgetSnapshot(remaining=10, max_no_improve=3)
    b.consume(improved=False)
    b.consume(improved=False)
    b.consume(improved=True)
    assert b.no_improve_streak == 0
    assert not b.is_exhausted
