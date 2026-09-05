"""Focused lifecycle boundaries for experiment-document projection."""

from types import SimpleNamespace

import pytest

from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.experiment_documents.models import (
    DOCUMENTS_STALE_MESSAGE,
    ProjectionOutcome,
)
from athena.research.contracts import ValidationResult
from athena.research.supervisor.experiment import PlanBest, PlanTurnResult
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.phases import PhaseMachine, _phase_failure_run_id
from athena.research.supervisor.plans import PlanInput, PlanState
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor
from test.unit.research.supervisor.test_supervisor import (
    _checkpoint_supervisor,
    _seed_skip_sota,
)


class RecordingDocuments:
    def __init__(self, outcome: ProjectionOutcome | None = None) -> None:
        self.outcome = outcome or ProjectionOutcome.success("a" * 64)
        self.stage_calls: list[dict[str, object]] = []
        self.rebuild_calls: list[dict[str, object]] = []

    def project_stage(self, event, **context):
        self.stage_calls.append({"event": dict(event), **context})
        return self.outcome

    def rebuild(self, **context):
        self.rebuild_calls.append(context)
        return self.outcome


def _supervisor(documents: RecordingDocuments, publisher) -> Supervisor:
    supervisor = object.__new__(Supervisor)
    supervisor.state = SimpleNamespace(
        validation={"status": "COMPLETED"},
        validation_skipped=True,
        task_understanding={"metric": "accuracy"},
    )
    supervisor.tree = object()
    supervisor._deps = SimpleNamespace(
        runtime=SimpleNamespace(documents=documents),
        phases=SimpleNamespace(publish=publisher),
        search=SimpleNamespace(direction="maximize"),
    )
    return supervisor


@pytest.mark.asyncio
async def test_rebuild_uses_canonical_state_and_publishes_sanitized_warning():
    documents = RecordingDocuments(ProjectionOutcome.stale())
    published = []

    async def publish(kind, payload):
        published.append((kind, payload))

    supervisor = _supervisor(documents, publish)

    await supervisor._rebuild_documents()

    assert documents.rebuild_calls == [
        {
            "tree": supervisor.tree,
            "validation": supervisor.state.validation,
            "validation_skipped": True,
            "task_understanding": supervisor.state.task_understanding,
            "direction": "maximize",
        }
    ]
    assert published == [
        (
            "output",
            {
                "source": "supervisor",
                "channel": "error",
                "text": DOCUMENTS_STALE_MESSAGE,
            },
        )
    ]


@pytest.mark.asyncio
async def test_warning_publisher_failure_does_not_replace_research_result(caplog):
    documents = RecordingDocuments(ProjectionOutcome.stale())

    async def publish(kind, payload):
        raise RuntimeError("publisher down")

    supervisor = _supervisor(documents, publish)

    await supervisor._rebuild_documents()

    assert len(documents.rebuild_calls) == 1
    assert "failed to publish document projection warning" in caplog.text


@pytest.mark.asyncio
async def test_stage_projection_receives_canonical_snapshot_and_fixed_warning():
    documents = RecordingDocuments(ProjectionOutcome.stale())
    published = []

    async def publish(kind, payload):
        published.append(payload)

    supervisor = _supervisor(documents, publish)
    machine = object.__new__(PhaseMachine)
    machine._owner = supervisor
    machine._deps = supervisor._deps

    await machine._project_stage(
        {
            "run_id": "exp_baseline",
            "stage": "baseline",
            "status": "SUCCEEDED",
            "metric": {"primary": 0.9},
            "reason": {"kind": "trusted_score", "summary": "saved"},
            "provenance": {"phase": "PREPARE"},
        }
    )

    assert documents.stage_calls[0]["event"]["status"] == "SUCCEEDED"
    assert documents.stage_calls[0]["validation"] is supervisor.state.validation
    assert published[0]["text"] == DOCUMENTS_STALE_MESSAGE


def test_phase_failure_ids_are_deterministic_and_stage_scoped():
    first = _phase_failure_run_id("baseline", ValueError("bad  input"))
    repeat = _phase_failure_run_id("baseline", ValueError("bad input"))
    different = _phase_failure_run_id("baseline", ValueError("other"))

    assert first == repeat
    assert first != different
    import re

    assert re.fullmatch(r"baseline-phase-failure-[0-9a-f]{12}", first)
    assert re.fullmatch(r"baseline-phase-failure-[0-9a-f]{12}", different)


def _prepare_result() -> PrepareResult:
    return PrepareResult(
        evaluator_ref="eval-ref",
        metric=0.91,
        commit="a" * 40,
        predictions_ref="predictions-ref",
        evidence_ref="evidence-ref",
        report_ref="report-ref",
    )


class CanonicalObservingDocuments(RecordingDocuments):
    """Projector double that verifies canonical disk state is already durable."""

    def __init__(self, supervisor, *, outcome=None):
        super().__init__(outcome)
        self.supervisor = supervisor
        self.observations: list[tuple[str, object, object]] = []

    def project_stage(self, event, **context):
        from athena.core.research_tree import ResearchTree
        from athena.research.supervisor.state import ResearchState

        tree_path = self.supervisor._deps.paths.tree_path
        tree = ResearchTree.load(tree_path) if tree_path.is_file() else None
        state = ResearchState.load(self.supervisor._deps.paths.state_path)
        self.observations.append((event["stage"], tree, state))
        return super().project_stage(event, **context)


@pytest.mark.asyncio
async def test_prepare_projects_only_after_tree_and_evaluator_state_are_saved(tmp_path):
    async def prepare():
        return _prepare_result()

    supervisor = _checkpoint_supervisor(tmp_path, run_prepare_phase=prepare)
    documents = CanonicalObservingDocuments(supervisor)
    supervisor._deps.runtime = supervisor._deps.runtime.__class__(
        store=supervisor._deps.runtime.store,
        agents=supervisor._deps.runtime.agents,
        workspaces=supervisor._deps.runtime.workspaces,
        documents=documents,
    )

    await supervisor._phases._run_prepare()

    stage, tree, state = documents.observations[0]
    assert stage == "baseline"
    assert tree is not None
    assert tree.best_experiment_id() == "exp_baseline"
    assert state.evaluator_ref == "eval-ref"


@pytest.mark.asyncio
async def test_validate_projects_after_completed_validation_is_saved(tmp_path):
    supervisor = _checkpoint_supervisor(tmp_path)

    async def prepare():
        return _prepare_result()

    supervisor._deps.phases.prepare = prepare
    await supervisor._phases._run_prepare()
    supervisor.state.phase = "VALIDATE"
    supervisor.state.status = "RUNNING"

    async def validate(_commit, _score):
        return ValidationResult(
            result_id="validation-1",
            status="COMPLETED",
            test_score=0.91,
            final_test_score=0.88,
            sota_commit="a" * 40,
        )

    supervisor._deps.phases.validation = validate
    documents = CanonicalObservingDocuments(supervisor)
    supervisor._deps.runtime = supervisor._deps.runtime.__class__(
        store=supervisor._deps.runtime.store,
        agents=supervisor._deps.runtime.agents,
        workspaces=supervisor._deps.runtime.workspaces,
        documents=documents,
    )

    await supervisor._phases._run_validation()

    stage, _tree, state = documents.observations[-1]
    assert stage == "final"
    assert state.phase == "COMPLETED"
    assert state.status == "COMPLETED"
    assert state.validation["result_id"] == "validation-1"


@pytest.mark.asyncio
async def test_skipped_validation_projects_after_completed_state_is_saved(tmp_path):
    supervisor = _checkpoint_supervisor(tmp_path)

    async def prepare():
        return _prepare_result()

    supervisor._deps.phases.prepare = prepare
    await supervisor._phases._run_prepare()
    supervisor.state.phase = "SEARCH"
    supervisor.state.status = "RUNNING"
    supervisor.configure_options(skip_validate=True)
    documents = CanonicalObservingDocuments(supervisor)
    supervisor._deps.runtime = supervisor._deps.runtime.__class__(
        store=supervisor._deps.runtime.store,
        agents=supervisor._deps.runtime.agents,
        workspaces=supervisor._deps.runtime.workspaces,
        documents=documents,
    )

    await supervisor._phases._finalize_without_validation()

    stage, _tree, state = documents.observations[-1]
    assert stage == "final"
    assert state.phase == "COMPLETED"
    assert state.status == "COMPLETED"
    assert state.validation_skipped is True


@pytest.mark.asyncio
async def test_phase_failure_saves_failed_state_and_preserves_original_error(tmp_path):
    published = []

    async def publish(_kind, payload):
        published.append(payload)

    async def fail_prepare():
        raise RuntimeError("prepare exploded")

    supervisor = _checkpoint_supervisor(
        tmp_path, run_prepare_phase=fail_prepare, publish_callback=publish
    )
    documents = CanonicalObservingDocuments(supervisor)
    supervisor._deps.runtime = supervisor._deps.runtime.__class__(
        store=supervisor._deps.runtime.store,
        agents=supervisor._deps.runtime.agents,
        workspaces=supervisor._deps.runtime.workspaces,
        documents=documents,
    )

    with pytest.raises(RuntimeError, match="prepare exploded"):
        await supervisor.start()

    stage, _tree, state = documents.observations[-1]
    assert stage == "baseline"
    assert state.phase == "PREPARE"
    assert state.status == "FAILED"
    assert any("prepare exploded" in payload["text"] for payload in published)
    failure_id = documents.stage_calls[-1]["event"]["run_id"]
    assert failure_id.startswith("baseline-phase-failure-")
    assert len(failure_id.rsplit("-", 1)[-1]) == 12


@pytest.mark.asyncio
async def test_resume_orders_plan_recovery_then_rebuild_then_new_work(tmp_path):
    supervisor = _checkpoint_supervisor(tmp_path)
    supervisor.state.phase = "SEARCH"
    order = []

    async def recover(_state=None):
        order.append("plan-recovery")
        return supervisor.state

    supervisor._plans.recover = recover
    documents = RecordingDocuments()

    def rebuild(**context):
        order.append("rebuild")
        documents.rebuild_calls.append(context)
        return documents.outcome

    documents.rebuild = rebuild
    supervisor._deps.runtime = supervisor._deps.runtime.__class__(
        store=supervisor._deps.runtime.store,
        agents=supervisor._deps.runtime.agents,
        workspaces=supervisor._deps.runtime.workspaces,
        documents=documents,
    )
    supervisor._phases.continue_phase = lambda: _record_async(order, "new-search")

    await supervisor.start()

    assert order == ["plan-recovery", "rebuild", "new-search"]


@pytest.mark.asyncio
async def test_prepare_start_rebuilds_once_before_prepare_adapter(tmp_path):
    order = []

    async def prepare():
        order.append("prepare")
        return _prepare_result()

    supervisor = _checkpoint_supervisor(tmp_path, run_prepare_phase=prepare)
    documents = RecordingDocuments()

    def rebuild(**context):
        order.append("rebuild")
        documents.rebuild_calls.append(context)
        return documents.outcome

    documents.rebuild = rebuild
    supervisor._deps.runtime = supervisor._deps.runtime.__class__(
        store=supervisor._deps.runtime.store,
        agents=supervisor._deps.runtime.agents,
        workspaces=supervisor._deps.runtime.workspaces,
        documents=documents,
    )
    supervisor._phases.continue_phase = lambda: _record_async(order, "continue")

    await supervisor.start()

    assert order[:2] == ["rebuild", "prepare"]
    assert order.count("rebuild") == 1


@pytest.mark.asyncio
async def test_search_settlement_keeps_canonical_result_when_projection_raises(
    tmp_path,
):
    supervisor = _checkpoint_supervisor(tmp_path)
    _seed_skip_sota(supervisor, tmp_path)
    store = supervisor._deps.runtime.store
    evaluator_ref = await store.put_text("evaluator")
    tree_ref = await store.put_text("tree")
    evidence_ref = await store.put_text("{}")
    context_ref = await store.put_text(
        PlanInput(
            evaluator_ref=evaluator_ref,
            tree_ref=tree_ref,
            reference_metric=0.71,
            reference_priority=0.0,
        ).model_dump_json()
    )
    best_ref = await store.put_text(
        PlanBest(
            metric=0.83,
            commit="candidate-commit",
            evidence_ref=evidence_ref,
        ).model_dump_json()
    )
    supervisor.tree.add_hypothesis(
        Hypothesis(
            id="h_candidate",
            statement="candidate",
            intervention="change model",
            expected_effect="improve score",
            parent_id="exp_baseline",
        )
    )
    supervisor.tree.add_experiment(
        "exp_h_candidate",
        Experiment(
            hypothesis_id="h_candidate",
            parent_id="exp_baseline",
            commit="candidate-commit",
            plan=ExperimentPlan(
                kind="search",
                change="candidate",
                run_config_ref=evaluator_ref,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path=str(tmp_path), branch="candidate", base_commit="baseline-commit"
            ),
            status=ExperimentStatus.RUNNING,
        ),
    )
    supervisor.state.phase = "SEARCH"
    supervisor.state.plans["h_candidate"] = PlanState(
        kind="SEARCH",
        context_ref=context_ref,
        turns_used=1,
        turn_limit=2,
        patience=1,
    )
    events = []
    settled = []
    reaped = []

    async def publish(kind, payload):
        events.append((kind, payload))

    async def on_settled(plan_id):
        settled.append(plan_id)

    class Agents:
        async def reap(self, plan_id):
            reaped.append(plan_id)

    class RaisingDocuments(RecordingDocuments):
        def project_stage(self, event, **context):
            persisted_tree = ResearchTree.load(supervisor._deps.paths.tree_path)
            persisted_state = ResearchState.load(supervisor._deps.paths.state_path)
            assert (
                persisted_tree.get_experiment("exp_h_candidate").status
                is ExperimentStatus.SUCCEEDED
            )
            assert persisted_tree.best_experiment_id() == "exp_h_candidate"
            assert (
                persisted_tree.get_hypothesis("h_candidate").status.value == "SUPPORTED"
            )
            assert "h_candidate" not in persisted_state.plans
            raise RuntimeError("derived projection failed")

    documents = RaisingDocuments()
    runtime = supervisor._deps.runtime
    supervisor._deps.runtime = runtime.__class__(
        store=runtime.store,
        agents=Agents(),
        workspaces=runtime.workspaces,
        documents=documents,
    )
    supervisor._deps.phases.publish = publish
    supervisor._deps.phases.on_plan_settled = on_settled

    await supervisor._plans.settle_plan(
        "h_candidate",
        best_ref,
        PlanTurnResult(kind="scored", next_state=supervisor.state.plans["h_candidate"]),
    )

    warnings = [
        payload
        for kind, payload in events
        if kind == "output" and payload.get("channel") == "error"
    ]
    assert len(warnings) == 1
    assert warnings[0]["text"] == DOCUMENTS_STALE_MESSAGE
    assert settled == ["h_candidate"]
    assert reaped == ["h_candidate"]


async def _record_async(order: list[str], item: str) -> None:
    order.append(item)
