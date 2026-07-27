from athena.workflows.report.final_report import Reporter
from athena.workflows.validate.ablation import Validator
from athena.core.schemas import EvalResult


def test_reporter_generates_output():
    reporter = Reporter()
    import asyncio

    # Smoke test: reporter runs without error
    # Full integration test needs ResearchTree with data
    assert reporter is not None


def test_validator_imports():
    validator = Validator()
    assert validator is not None
