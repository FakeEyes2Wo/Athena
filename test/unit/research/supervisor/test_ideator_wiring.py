"""Ideator wiring: Supervisor.register_hypotheses + GENERATE 经 run_ideator_turn。"""

from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus, ResearchTree
from athena.core.workspace import GitWorkBranch
from athena.research.supervisor.recovery import Recovery
from athena.research.supervisor.scheduler import Scheduler
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import Supervisor


async def _make_supervisor(tmp_path: Path, *, run_ideator_turn=None) -> Supervisor:
    """Build a SEARCH Supervisor with a baseline SOTA and no plans."""
    store = LocalArtifactStore(tmp_path / "artifacts")
    evaluator_ref = await store.put_text('{"frozen":true}')
    evidence_ref = await store.put_text("baseline evidence")
    tree = ResearchTree()
    tree.add_hypothesis(
        Hypothesis(
            id="baseline",
            statement="baseline",
            intervention="fit baseline",
            expected_effect="establish reference",
            priority=42.0,
        )
    )
    tree.add_experiment(
        "exp_baseline",
        Experiment(
            hypothesis_id="baseline",
            commit="c0",
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                run_config_ref=evaluator_ref,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(path=str(tmp_path), branch="main", base_commit="c0"),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_baseline", primary=0.8, per_sample=evidence_ref
            ),
        ),
    )
    tree.set_sota("exp_baseline")
    state = ResearchState(
        status="RUNNING", phase="SEARCH", search_limit=10, concurrency=4
    )

    async def unused_plan_turn(_plan_id, _state):
        raise AssertionError("Plan turn must not run")

    async def unused_supervisor_turn(_text):
        raise AssertionError("Supervisor turn must not run")

    async def publish(_kind, _payload):
        return None

    return Supervisor(
        project_root=tmp_path,
        state=state,
        tree=tree,
        store=store,
        agents=None,
        workspaces=None,
        scheduler=Scheduler(),
        recovery=Recovery(),
        evaluator_ref=evaluator_ref,
        run_plan_turn=unused_plan_turn,
        run_supervisor_turn=unused_supervisor_turn,
        publish=publish,
        run_ideator_turn=run_ideator_turn,
    )


@pytest.mark.asyncio
async def test_register_hypotheses_batches_under_current_sota(tmp_path: Path) -> None:
    supervisor = await _make_supervisor(tmp_path)
    batch = [
        Hypothesis(
            statement="scale features",
            intervention="standardize numeric inputs",
            expected_effect="raise primary metric",
        ),
        Hypothesis(
            statement="add interaction",
            intervention="multiply age and income",
            expected_effect="raise primary metric",
        ),
    ]
    result = await supervisor.register_hypotheses(batch)

    assert len(result["hypothesis_ids"]) == 2
    pending = [
        hypothesis
        for hypothesis in supervisor.tree.pending_hypotheses()
        if hypothesis.id in result["hypothesis_ids"]
    ]
    assert len(pending) == 2
    for hypothesis in pending:
        assert hypothesis.parent_id == "exp_baseline"
        assert hypothesis.status == "PROPOSED"
        assert hypothesis.priority == 42.0  # 经 scheduler 从父假设播种


@pytest.mark.asyncio
async def test_generate_runs_ideator_turn_and_registers(tmp_path: Path) -> None:
    calls: list[int] = []

    async def fake_ideator(count: int) -> list[Hypothesis]:
        calls.append(count)
        return [
            Hypothesis(
                statement=f"h{i}",
                intervention=f"change {i}",
                expected_effect="raise primary metric",
            )
            for i in range(count)
        ]

    supervisor = await _make_supervisor(tmp_path, run_ideator_turn=fake_ideator)

    generated = await supervisor._fill_slots()

    assert generated is True
    assert calls == [4]  # free_slots=4 → GENERATE(4)
    new_pending = [
        hypothesis
        for hypothesis in supervisor.tree.pending_hypotheses()
        if hypothesis.intervention.startswith("change")
    ]
    assert len(new_pending) == 4
    for hypothesis in new_pending:
        assert hypothesis.parent_id == "exp_baseline"


@pytest.mark.asyncio
async def test_generate_without_ideator_falls_back_to_supervisor(
    tmp_path: Path,
) -> None:
    supervisor = await _make_supervisor(tmp_path)
    with pytest.raises(AssertionError, match="Supervisor turn must not run"):
        await supervisor._fill_slots()
