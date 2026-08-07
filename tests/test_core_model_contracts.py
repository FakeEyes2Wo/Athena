import athena.evaluation.types as evaluation_types

from athena.core.contracts import EventEnvelope, new_id
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.core.thread_models import AgentTask, AthenaThread, AthenaTurn
from athena.data.types import DataCard
from athena.evaluation.types import ComparisonVerdict, EvalResult, EvalSpec, MetricDef
from athena.research.models import MetricSpec, TaskMetaData


def test_canonical_contracts_construct_real_domain_records() -> None:
    assert new_id("run").startswith("run_")
    envelope = EventEnvelope(
        kind="experiment.started",
        source="orchestrator",
        payload={"ref": "artifact://input"},
        state_version=0,
    )
    assert envelope.payload["ref"] == "artifact://input"

    task = AgentTask(task_id="task-1", agent_type="planner", command="plan")
    thread = AthenaThread(
        thread_id="thread-1",
        session_id="session-1",
        status="running",
        context_ref="artifact://context",
    )
    turn = AthenaTurn(
        turn_id="turn-1",
        thread_id=thread.thread_id,
        request_ref="artifact://request",
        status="running",
    )
    assert task.context_refs == []
    assert turn.thread_id == thread.thread_id


def test_domain_models_retain_validation_and_serialization() -> None:
    hypothesis = Hypothesis(
        statement="Use stronger regularization",
        intervention="Increase weight decay",
        expected_effect="Improve validation accuracy",
    )
    plan = ExperimentPlan(
        kind="search",
        change="Increase weight decay",
        run_config_ref="artifact://run-config",
        budget={"trials": 1},
        acceptance_rule="Primary metric improves",
    )
    evaluation = EvalResult(
        experiment_id="experiment-1",
        primary=0.8,
        per_sample="artifact://samples",
    )
    verdict = ComparisonVerdict(winner="candidate", p_value=0.01)
    assert hypothesis.status == "PROPOSED"
    assert plan.kind == "search"
    assert evaluation.primary == 0.8
    assert verdict.winner == "candidate"


def test_evaluation_types_exports_the_canonical_contracts() -> None:
    assert evaluation_types.__all__ == [
        "ComparisonVerdict",
        "EvaluationInputs",
        "EvalResult",
        "EvalSpec",
        "MetricDef",
    ]
