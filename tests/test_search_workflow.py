"""Tests for SEARCH workflow: code routing, supervisor decisions."""

import pytest
from athena.core.schemas import MetricDef, EvalSpec
from athena.core.budget import BudgetSnapshot, RunMode
from athena.core.research.research_tree import ResearchTree
from athena.execution.supervisor import Supervisor, Decision
from athena.workflows.search.code_agent import CodeRouter


def test_code_router_routes_small_change():
    from athena.core.schemas import Hypothesis

    h = Hypothesis(
        statement="test", intervention="add dropout", expected_effect="+0.01"
    )
    router = CodeRouter()
    assert router.route(h) == "qoder"


def test_code_router_routes_large_change():
    from athena.core.schemas import Hypothesis

    h = Hypothesis(
        statement="test",
        intervention="build an entirely new transformer architecture from scratch",
        expected_effect="+0.05",
    )
    router = CodeRouter()
    assert router.route(h) == "codex"


def test_supervisor_stops_on_streak():
    from athena.core.schemas import ComparisonVerdict
    from athena.core.budget import BudgetSnapshot

    s = Supervisor()
    budget = BudgetSnapshot(
        remaining=10, no_improve_streak=4, max_no_improve=5, is_exhausted=False
    )
    decision = s.decide(ComparisonVerdict(winner="baseline", p_value=0.5), budget)
    assert decision.action == "STOP"


def test_supervisor_accepts_improvement():
    from athena.core.schemas import ComparisonVerdict
    from athena.core.budget import BudgetSnapshot

    s = Supervisor()
    budget = BudgetSnapshot(remaining=5, max_no_improve=5)
    decision = s.decide(ComparisonVerdict(winner="candidate", p_value=0.01), budget)
    assert decision.action == "ACCEPT"
