"""Autonomous ResearchRuntime phase integration."""

import asyncio
import inspect
import json
from pathlib import Path

import pytest

from athena.core.artifact_store import ArtifactNotFoundError
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus
from athena.core.workspace import GitWorkBranch
from athena.execution.runtime import CommandResult
from athena.research.contracts import DataScriptBundle, ValidationResult
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.prepare import PrepareResult
from athena.research.supervisor.state import ResearchState


async def _eventually(predicate, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_all_phases_use_one_authority(tmp_path: Path, monkeypatch) -> None:
    state_writers: set[str] = set()
    original_save = ResearchState.save

    def record_state_writer(state: ResearchState, path: str | Path) -> Path:
        owner = inspect.currentframe().f_back.f_locals.get("self")
        state_writers.add(type(owner).__name__)
        return original_save(state, path)

    monkeypatch.setattr(ResearchState, "save", record_state_writer)

    prepare_evidence = "sha256:" + "1" * 64
    evaluator_ref = "sha256:" + "2" * 64
    predictions_ref = "sha256:" + "3" * 64
    report_ref = "sha256:" + "4" * 64

    async def prepare() -> PrepareResult:
        return PrepareResult(
            evaluator_ref=evaluator_ref,
            metric=0.71,
            commit="prepare-commit",
            predictions_ref=predictions_ref,
            evidence_ref=prepare_evidence,
            report_ref=report_ref,
        )

    async def validate(_sota_commit: str, _metric: float) -> ValidationResult:
        return ValidationResult(
            result_id="validation-key",
            status="COMPLETED",
            test_score=0.71,
            final_test_score=0.70,
            sota_commit="prepare-commit",
            validation_commit="validation-commit",
            predictions_ref=predictions_ref,
            evidence_ref=prepare_evidence,
        )

    runtime = ResearchRuntime(
        project_root=tmp_path,
        search_limit=0,
        concurrency=1,
        auto_validate=True,
        prepare_phase=prepare,
        validation_phase=validate,
    )
    events: list[tuple[str, dict[str, object]]] = []
    runtime.subscribe(lambda kind, payload: events.append((kind, payload)))

    await runtime.start()
    await _eventually(
        lambda: any(
            kind == "state" and payload["phase"] == "COMPLETED"
            for kind, payload in events
        )
    )

    phases: list[str] = []
    for kind, payload in events:
        phase = str(payload.get("phase"))
        if kind == "state" and (not phases or phase != phases[-1]):
            phases.append(phase)
    assert phases == ["PREPARE", "SEARCH", "VALIDATE", "COMPLETED"]
    assert runtime.state.status == "COMPLETED"
    assert state_writers == {"Supervisor"}
    assert set(kind for kind, _payload in events) == {"output", "state"}
    await runtime.aclose()


@pytest.mark.asyncio
async def test_default_prepare_adapter_uses_existing_phase_runner(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}
    evaluator_captured = {}
    frozen_ref = {}

    async def run_evaluator_plan(**kwargs):
        evaluator_captured.update(kwargs)
        frozen_ref["evaluator"] = await kwargs["store"].put_text(
            DataScriptBundle(
                bundle_id="prepare-evaluator", entrypoint="evaluate.py"
            ).model_dump_json()
        )
        return frozen_ref["evaluator"]

    async def run_prepare_plan(**kwargs):
        captured.update(kwargs)
        return PrepareResult(
            evaluator_ref=kwargs["evaluator_ref"],
            metric=0.71,
            commit=kwargs["workspace"].base_commit,
            predictions_ref=await kwargs["store"].put_text("predictions"),
            evidence_ref=await kwargs["store"].put_text("evidence"),
            report_ref=await kwargs["store"].put_text("report"),
        )

    monkeypatch.setattr(
        "athena.research.prepare_phase.run_evaluator_plan",
        run_evaluator_plan,
        raising=False,
    )
    monkeypatch.setattr(
        "athena.research.prepare_phase.run_prepare_plan",
        run_prepare_plan,
        raising=False,
    )
    runtime = ResearchRuntime(project_root=tmp_path, task="predict survival")
    runtime.register_supervisor(provider=object())
    await runtime._git.init()
    runtime._agents.start()

    result = await runtime._phase_runner.run_prepare_phase()

    assert result.metric == pytest.approx(0.71)
    assert evaluator_captured["agents"] is runtime._agents
    assert evaluator_captured["scripts"] is runtime._scripts
    assert evaluator_captured["store"] is runtime._store
    assert evaluator_captured["execution"] is runtime._execution
    assert evaluator_captured["task"] == "predict survival"
    assert evaluator_captured["evaluator_dir"] == (
        runtime._root / "workspaces" / "evaluator"
    )
    assert captured["agents"] is runtime._agents
    assert captured["evaluator"] is runtime._evaluator
    assert captured["git"] is runtime._git
    assert captured["execution"] is runtime._execution
    assert captured["store"] is runtime._store
    assert captured["task"] == "predict survival"
    assert captured["workspace"].branch == "athena/prepare"
    assert captured["evaluator_ref"] == frozen_ref["evaluator"]
    assert json.loads(await runtime._store.get_text(captured["tree_ref"])) == (
        runtime.tree.to_dict()
    )
    await runtime.aclose()


@pytest.mark.asyncio
async def test_prepare_phase_reuses_frozen_evaluator_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    async def run_evaluator_plan(**kwargs):
        del kwargs
        raise AssertionError("evaluator must be skipped when a checkpoint exists")

    async def run_prepare_plan(**kwargs):
        return PrepareResult(
            evaluator_ref=kwargs["evaluator_ref"],
            metric=0.71,
            commit=kwargs["workspace"].base_commit,
            predictions_ref=await kwargs["store"].put_text("predictions"),
            evidence_ref=await kwargs["store"].put_text("evidence"),
            report_ref=await kwargs["store"].put_text("report"),
        )

    monkeypatch.setattr(
        "athena.research.prepare_phase.run_evaluator_plan",
        run_evaluator_plan,
        raising=False,
    )
    monkeypatch.setattr(
        "athena.research.prepare_phase.run_prepare_plan",
        run_prepare_plan,
        raising=False,
    )
    runtime = ResearchRuntime(project_root=tmp_path, task="predict survival")
    runtime.register_supervisor(provider=object())
    await runtime._git.init()
    runtime._agents.start()
    frozen_ref = await runtime._store.put_text(
        DataScriptBundle(
            bundle_id="prepare-evaluator", entrypoint="evaluate.py"
        ).model_dump_json()
    )
    runtime.state.evaluator_ref = frozen_ref
    runtime.supervisor._evaluator_ref = frozen_ref
    events: list[tuple[str, dict[str, object]]] = []
    runtime.subscribe(lambda kind, payload: events.append((kind, payload)))

    result = await runtime._phase_runner.run_prepare_phase()

    assert result.evaluator_ref == frozen_ref
    assert any(
        kind == "output" and "复用已冻结的评估器断点" in str(payload.get("text"))
        for kind, payload in events
    )
    assert runtime.state.evaluator_ref == frozen_ref
    await runtime.aclose()


@pytest.mark.asyncio
async def test_default_validation_adapter_uses_frozen_inputs_and_supervisor_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}

    async def run_validation_plan(**kwargs):
        captured.update(kwargs)
        checkpoint_ref = await kwargs["store"].put_text("validation checkpoint")
        await kwargs["checkpoint"](checkpoint_ref)
        return ValidationResult(
            result_id=kwargs["input"].validation_key,
            status="COMPLETED",
            test_score=kwargs["input"].reference_metric,
            final_test_score=0.79,
            sota_commit=kwargs["input"].sota_commit,
            validation_commit=kwargs["input"].sota_commit,
            predictions_ref=await kwargs["store"].put_text("final predictions"),
            evidence_ref=await kwargs["store"].put_text("final evidence"),
        )

    monkeypatch.setattr(
        "athena.research.phase_runner.run_validation_plan",
        run_validation_plan,
        raising=False,
    )
    runtime = ResearchRuntime(project_root=tmp_path, direction="minimize")
    runtime.register_supervisor(provider=object())
    base_commit = await runtime._git.init()
    runtime._agents.start()
    evaluator_ref = await runtime._store.put_text(
        DataScriptBundle(
            bundle_id="frozen-evaluator", entrypoint="eval.py"
        ).model_dump_json()
    )
    runtime.supervisor._evaluator_ref = evaluator_ref
    evidence_ref = await runtime._store.put_text("baseline evidence")
    runtime.tree.add_hypothesis(
        Hypothesis(
            id="baseline",
            statement="baseline",
            intervention="fit baseline",
            expected_effect="establish reference",
        )
    )
    runtime.tree.add_experiment(
        "exp_baseline",
        Experiment(
            hypothesis_id="baseline",
            commit=base_commit,
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                run_config_ref=evaluator_ref,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path=str(tmp_path), branch="main", base_commit=base_commit
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_baseline",
                primary=0.82,
                per_sample=evidence_ref,
            ),
        ),
    )
    runtime.tree.set_sota("exp_baseline")

    result = await runtime._phase_runner.run_validation_phase(base_commit, 0.82)

    validation_input = captured["input"]
    assert result.final_test_score == pytest.approx(0.79)
    assert validation_input.sota_commit == base_commit
    assert validation_input.reference_metric == pytest.approx(0.82)
    assert validation_input.direction == "minimize"
    assert validation_input.final_evaluator_ref == evaluator_ref
    assert captured["workspace"].branch == "athena/validate"
    assert callable(captured["independent_review"])
    assert runtime.state.validation == {
        "result_ref": await runtime._store.put_text("validation checkpoint")
    }
    await runtime.aclose()


@pytest.mark.asyncio
async def test_completed_command_emits_one_redacted_full_output_reference(
    tmp_path: Path,
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    events: list[tuple[str, dict[str, object]]] = []
    runtime.subscribe(lambda kind, payload: events.append((kind, payload)))
    secret = "sk-complete-output-secret"
    full = f"stdout-tail\nstderr-tail\nOPENAI_API_KEY={secret}\n" + ("x" * 700)
    raw_ref = await runtime._store.put_text(full)

    await runtime._events_bus.project_agent_event(
        "hyp_1",
        "command/stdout",
        "exec:run",
        {"delta": "ignored stream delta"},
    )
    await runtime._events_bus.project_agent_event(
        "hyp_1",
        "command/stderr",
        "exec:run",
        {"delta": "ignored stream delta"},
    )
    await runtime._events_bus.project_agent_event(
        "hyp_1",
        "command/completed",
        "exec:run",
        CommandResult(
            ok=False,
            stdout="o" * 500,
            stderr="error " + ("错" * 200),
            exit_code=1,
            truncated=True,
            output_ref=raw_ref,
        ).to_dict(),
    )

    outputs = [payload for kind, payload in events if kind == "output"]
    assert len(outputs) == 1
    event = outputs[0]
    assert event["channel"] == "stderr"
    assert len(str(event["text"]).encode("utf-8")) <= 512
    assert event["artifact_ref"] is not None
    redacted_full = await runtime._store.get_text(str(event["artifact_ref"]))
    assert secret not in redacted_full
    assert "[REDACTED]" in redacted_full
    assert "stdout-tail" in redacted_full
    assert "stderr-tail" in redacted_full
    with pytest.raises(ArtifactNotFoundError):
        await runtime._store.get_text(raw_ref)
    assert [path for path in runtime._store._root.rglob("*") if path.is_file()] == [
        runtime._store.path_for(str(event["artifact_ref"]))
    ]
    await runtime.aclose()
