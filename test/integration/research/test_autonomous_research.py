"""Autonomous ResearchRuntime phase integration."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import athena.research.supervisor.phases as phases_module

from athena.core.artifact_store import ArtifactNotFoundError
from athena.gui.service import GuiService
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus
from athena.core.workspace import GitWorkBranch
from athena.execution.runtime import CommandResult
from athena.research.contracts import (
    DataScriptBundle,
    EvaluatorDescriptor,
    ValidationResult,
)
from athena.research.prepare import orchestrator
from athena.research.prepare.authority import (
    BaselineAuthorityConflict,
    PrepareAttestation,
    SealedBaseline,
    VerifiedBaselineBundle,
)
from athena.research.prepare.baseline_research import (
    BaselineVerification,
    VerifiedBaseline,
    design_sha256,
    load_baseline_artifacts,
    research_sha256,
    verification_bytes,
    write_verification,
)
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.prepare import PrepareResult


def _write_verified_baseline_fixture(root: Path) -> VerifiedBaseline:
    """Seed one complete offline research/design/verification trio."""
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "dataset": {
            "modality": "image",
            "task_type": "classification",
            "input_scale": "paired 224x224 images",
            "regime": "small",
            "facts": [
                {
                    "field": field,
                    "value": value,
                    "evidence": {
                        "kind": "eda",
                        "reference": f"EDA_HANDOFF.md#{field}",
                        "claim": claim,
                    },
                }
                for field, value, claim in (
                    ("labeled_samples", 480, "480 labeled images"),
                    ("effective_training_units", 120, "120 independent units"),
                    ("group_count", 120, "120 independent groups"),
                    ("class_count", 5, "5 target classes"),
                    ("minority_class_samples", 32, "32 minority-class samples"),
                )
            ],
            "rationale": "Grouped labels are limited relative to pretrained capacity.",
        },
        "training": {
            "strategy": "partial_finetune",
            "pretrained": {
                "status": "available",
                "representation": "ImageNet encoder",
                "evidence": {
                    "kind": "source",
                    "reference": "https://arxiv.org/abs/1512.03385",
                    "claim": "The selected method provides pretrained weights.",
                },
            },
            "safeguards": None,
            "scratch_scale": None,
        },
        "candidates": [
            {
                "candidate_id": "resnet-transfer",
                "title": "Deep Residual Learning for Image Recognition",
                "method": "pretrained ResNet feature extractor",
                "source_url": "https://arxiv.org/abs/1512.03385",
                "source_kind": "paper",
                "paper_locator": "doi:10.1109/CVPR.2016.90",
                "repository_url": "https://github.com/pytorch/vision.git",
                "publication_year": 2016,
                "claimed_citation_count": 100000,
                "relevance": "A standard transfer baseline for small image datasets.",
            },
            {
                "candidate_id": "linear-probe",
                "title": "PyTorch transfer learning tutorial",
                "method": "frozen visual features with a linear head",
                "source_url": (
                    "https://docs.pytorch.org/tutorials/beginner/"
                    "transfer_learning_tutorial.html"
                ),
                "source_kind": "technical_reference",
                "paper_locator": None,
                "repository_url": "https://github.com/pytorch/tutorials.git",
                "publication_year": None,
                "claimed_citation_count": None,
                "relevance": "A conservative alternative for scarce labels.",
            },
        ],
        "decisions": [
            {
                "candidate_id": "resnet-transfer",
                "decision": "selected",
                "reason": "best fit",
            },
            {
                "candidate_id": "linear-probe",
                "decision": "rejected",
                "reason": "less adaptive",
            },
        ],
        "selected_candidate_id": "resnet-transfer",
        "search": {
            "queries": [
                "small image classification transfer baseline GitHub",
                "authoritative pretrained image baseline paper",
            ],
            "one_candidate": None,
        },
        "limitations": [],
    }
    (root / "BASELINE_RESEARCH.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "BASELINE_DESIGN.md").write_text(
        "# Baseline\n\nSelected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n",
        encoding="utf-8",
    )
    artifacts = load_baseline_artifacts(root)
    verification = BaselineVerification(
        schema_version=2,
        research_sha256=research_sha256(artifacts.raw_research),
        design_sha256=design_sha256(artifacts.raw_design),
        selected_candidate_id=artifacts.selected.candidate_id,
        route="git",
        verified_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        repository_url=str(artifacts.selected.repository_url),
        commit="a" * 40,
        attempts=[{"route": "git", "success": True, "diagnostic": "verified"}],
    )
    write_verification(root, verification)
    return VerifiedBaseline(
        artifacts=artifacts,
        verification=verification,
        verification_bytes=verification_bytes(verification),
        authority_generation=0,
    )


def _bundle_for_verified(verified: VerifiedBaseline) -> VerifiedBaselineBundle:
    return VerifiedBaselineBundle(
        research_bytes=verified.artifacts.raw_research,
        design_bytes=verified.artifacts.raw_design,
        verification_bytes=verified.verification_bytes,
        verification=verified.verification,
    )


class _MemoryBaselineAuthorityStore:
    """Test-only external memory shared across one runtime lifecycle."""

    def __init__(self) -> None:
        self.sealed: SealedBaseline | None = None

    async def load(self) -> SealedBaseline | None:
        return self.sealed

    async def seal(
        self,
        bundle: VerifiedBaselineBundle,
        *,
        expected_generation: int | None,
    ) -> SealedBaseline:
        current = None if self.sealed is None else self.sealed.generation
        if current != expected_generation:
            raise BaselineAuthorityConflict("baseline generation changed")
        self.sealed = SealedBaseline(
            generation=0 if current is None else current + 1,
            bundle=bundle,
        )
        return self.sealed

    async def attest_prepare(
        self,
        evidence: PrepareAttestation,
        *,
        expected_generation: int,
    ) -> SealedBaseline:
        if self.sealed is None or self.sealed.generation != expected_generation:
            raise BaselineAuthorityConflict("baseline generation changed")
        self.sealed = SealedBaseline(
            generation=expected_generation + 1,
            bundle=self.sealed.bundle,
            attestation=evidence,
        )
        return self.sealed


def _install_verified_prepare_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def asserted_eda(_runtime, workspace, _handoff, _task) -> bool:
        root = Path(workspace.path)
        root.mkdir(parents=True, exist_ok=True)
        (root / "EDA_HANDOFF.md").write_text(
            "# EDA Handoff\n\n480 labels across 120 independent groups.\n",
            encoding="utf-8",
        )
        assert (root / "EDA_HANDOFF.md").is_file()
        return True

    async def verified_design(
        runtime, workspace, _task, eda_ready, _handoff
    ) -> VerifiedBaseline:
        root = Path(workspace.path)
        assert eda_ready is True
        assert (root / "EDA_HANDOFF.md").is_file()
        verified = _write_verified_baseline_fixture(root)
        sealed = await runtime.baseline_authority.seal(
            _bundle_for_verified(verified), expected_generation=None
        )
        assert sealed.generation == verified.authority_generation
        return verified

    monkeypatch.setattr(orchestrator, "prepare_eda", asserted_eda)
    monkeypatch.setattr(orchestrator, "prepare_baseline_design", verified_design)


def _assert_verified_prepare_task(task: object) -> None:
    rendered = str(task)
    for filename in (
        "BASELINE_RESEARCH.json",
        "BASELINE_RESEARCH_VERIFICATION.json",
        "BASELINE_DESIGN.md",
    ):
        assert filename in rendered
    assert "Selected candidate: resnet-transfer" in rendered
    assert "Training strategy: partial_finetune" in rendered


async def _eventually(predicate, timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_all_phases_share_one_durable_state(tmp_path: Path) -> None:
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
    assert runtime.state is runtime.services.durable.state
    assert runtime.supervisor.state is runtime.state
    assert set(kind for kind, _payload in events) == {"output", "state"}
    exp_docs = tmp_path / ".athena" / "exp_docs"
    assert (exp_docs / "runs" / "exp_baseline.json").is_file()
    assert (exp_docs / "runs" / "validation-key.json").is_file()
    assert json.loads((exp_docs / "final.json").read_text(encoding="utf-8"))["metric"][
        "primary"
    ] == pytest.approx(0.70)
    assert (exp_docs / "FINAL_REPORT.md").is_file()
    assert (exp_docs / "OPTIMIZATION.md").is_file()
    final_report = (exp_docs / "FINAL_REPORT.md").read_text(encoding="utf-8")
    assert "final_test_score" not in final_report
    assert "generalization_gap" not in final_report
    await runtime.aclose()


@pytest.mark.asyncio
async def test_skip_validate_completes_from_search_without_calling_validation(
    tmp_path: Path,
) -> None:
    validation_calls: list[tuple[str, float]] = []
    evidence_ref = "sha256:" + "1" * 64
    evaluator_ref = "sha256:" + "2" * 64
    predictions_ref = "sha256:" + "3" * 64
    report_ref = "sha256:" + "4" * 64

    async def prepare() -> PrepareResult:
        return PrepareResult(
            evaluator_ref=evaluator_ref,
            metric=0.71,
            commit="prepare-commit",
            predictions_ref=predictions_ref,
            evidence_ref=evidence_ref,
            report_ref=report_ref,
        )

    async def validate(commit: str, metric: float):
        validation_calls.append((commit, metric))
        raise AssertionError("VALIDATE must not run")

    runtime = ResearchRuntime(
        project_root=tmp_path,
        task="improve the trusted baseline",
        search_limit=0,
        auto_validate=True,
        skip_validate=True,
        auto_confirm=True,
        prepare_phase=prepare,
        validation_phase=validate,
    )
    events: list[tuple[str, dict[str, object]]] = []
    runtime.subscribe(lambda kind, data: events.append((kind, data)))

    lifecycle = await runtime.start()
    await asyncio.wait_for(asyncio.shield(lifecycle), timeout=5)

    assert validation_calls == []
    assert runtime.state.phase == "COMPLETED"
    assert runtime.state.status == "COMPLETED"
    assert runtime.state.validation is None
    assert runtime.state.validation_skipped is True
    phases: list[str] = []
    for kind, payload in events:
        phase = str(payload.get("phase"))
        if kind == "state" and (not phases or phase != phases[-1]):
            phases.append(phase)
    assert phases == ["PREPARE", "SEARCH", "COMPLETED"]
    assert any(
        "VALIDATE " in str(payload.get("text"))
        for kind, payload in events
        if kind == "output"
    )
    assert any(
        str(payload.get("text", "")).startswith("SEARCH 已完成；")
        for kind, payload in events
        if kind == "output"
    )
    exp_docs = tmp_path / ".athena" / "exp_docs"
    assert (exp_docs / "FINAL_REPORT.md").is_file()
    assert (exp_docs / "OPTIMIZATION.md").is_file()
    final = json.loads((exp_docs / "final.json").read_text(encoding="utf-8"))
    assert final["status"] == "SKIPPED"
    assert final["metric"]["primary"] is None
    assert final["metric"]["reference"] == pytest.approx(0.71)
    await runtime.aclose()


@pytest.mark.asyncio
async def test_completed_skip_report_uses_durable_marker_after_live_preference_changes(
    tmp_path: Path,
) -> None:
    """A reopened run remains historically skipped after the setting is toggled off."""

    async def prepare() -> PrepareResult:
        return PrepareResult(
            evaluator_ref="sha256:" + "2" * 64,
            metric=0.71,
            commit="prepare-commit",
            predictions_ref="sha256:" + "3" * 64,
            evidence_ref="sha256:" + "1" * 64,
            report_ref="sha256:" + "4" * 64,
        )

    async def validate(_commit: str, _metric: float) -> ValidationResult:
        raise AssertionError("historical skipped run must not validate")

    runtime = ResearchRuntime(
        project_root=tmp_path,
        task="improve the trusted baseline",
        search_limit=0,
        auto_validate=True,
        skip_validate=True,
        auto_confirm=True,
        prepare_phase=prepare,
        validation_phase=validate,
    )
    lifecycle = await runtime.start()
    await asyncio.wait_for(asyncio.shield(lifecycle), timeout=5)
    assert runtime.state.validation_skipped is True

    # This is a live setting change, not a rewrite of the completed run marker.
    await runtime.apply_settings({"skip_validate": False})
    assert runtime.settings()["skip_validate"] is False
    await runtime.aclose()

    reopened = ResearchRuntime(
        project_root=tmp_path,
        skip_validate=False,
        validation_phase=validate,
    )
    try:
        assert reopened.state.validation_skipped is True
        report = (await GuiService(reopened).generate_report())["report"]
        assert "VALIDATE \u5df2\u8df3\u8fc7" in report
        assert "final_test_score" not in report
        assert "generalization_gap" not in report
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_skip_finalization_retries_after_report_failure_without_duplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validation_calls: list[tuple[str, float]] = []
    evidence_ref = "sha256:" + "1" * 64

    async def prepare() -> PrepareResult:
        return PrepareResult(
            evaluator_ref="sha256:" + "2" * 64,
            metric=0.71,
            commit="prepare-commit",
            predictions_ref="sha256:" + "3" * 64,
            evidence_ref=evidence_ref,
            report_ref="sha256:" + "4" * 64,
        )

    async def validate(commit: str, metric: float):
        validation_calls.append((commit, metric))
        raise AssertionError("VALIDATE must not run")

    original_writer = phases_module.write_reports
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("final report unavailable")
        return original_writer(*args, **kwargs)

    monkeypatch.setattr(phases_module, "write_reports", fail_once)
    runtime = ResearchRuntime(
        project_root=tmp_path,
        task="improve the trusted baseline",
        search_limit=0,
        auto_confirm=True,
        skip_validate=True,
        prepare_phase=prepare,
        validation_phase=validate,
    )
    lifecycle = await runtime.start()
    await asyncio.wait_for(asyncio.shield(lifecycle), timeout=5)
    assert runtime.state.phase == "SEARCH"
    assert runtime.state.status == "FAILED"
    assert (
        json.loads((tmp_path / ".athena" / "state.json").read_text())["status"]
        == "FAILED"
    )

    monkeypatch.setattr(phases_module, "write_reports", original_writer)
    await runtime.resume_current_task()
    retry = runtime.session.lifecycle.task
    assert retry is not None
    await asyncio.wait_for(asyncio.shield(retry), timeout=5)

    assert runtime.state.phase == "COMPLETED"
    assert runtime.state.status == "COMPLETED"
    assert validation_calls == []
    assert (
        len(
            list(
                (tmp_path / ".athena" / "exp_docs" / "runs").glob("final-skipped.json")
            )
        )
        == 1
    )
    await runtime.aclose()


@pytest.mark.asyncio
async def test_default_prepare_adapter_uses_existing_phase_runner(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}
    evaluator_calls = []
    frozen_ref = {}

    async def run_evaluator_plan(**kwargs):
        evaluator_calls.append(kwargs)
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
        "athena.research.prepare.evaluator.run_evaluator_plan",
        run_evaluator_plan,
        raising=False,
    )
    monkeypatch.setattr(
        "athena.research.prepare.baseline.run_prepare_plan",
        run_prepare_plan,
        raising=False,
    )
    authority = _MemoryBaselineAuthorityStore()
    runtime = ResearchRuntime(
        project_root=tmp_path,
        task="predict survival",
        baseline_authority=authority,
    )
    runtime.register_supervisor(provider=object())
    await runtime.git.init()
    runtime.agents.start()
    _install_verified_prepare_gate(monkeypatch)

    result = await runtime.services.workflow.phases.run_prepare_phase()

    evaluator_captured = next(
        call
        for call in evaluator_calls
        if Path(call["evaluator_dir"]).parent.name == "evaluator"
    )
    assert result.metric == pytest.approx(0.71)
    assert evaluator_captured["agents"] is runtime.agents
    assert evaluator_captured["scripts"] is runtime.scripts
    assert evaluator_captured["store"] is runtime.store
    assert evaluator_captured["execution"] is runtime.execution
    assert evaluator_captured["task"].startswith("predict survival")
    assert "SEARCH partition" in evaluator_captured["task"]
    assert evaluator_captured["evaluator_dir"] == (
        runtime.root / "workspaces" / "evaluator" / "evaluate"
    )
    assert captured["agents"] is runtime.agents
    assert captured["evaluator"] is runtime.evaluator
    assert captured["git"] is runtime.git
    assert captured["execution"] is runtime.execution
    assert captured["store"] is runtime.store
    assert captured["task"].startswith("predict survival")
    assert captured["workspace"].branch == "athena/prepare"
    assert captured["evaluator_ref"] == frozen_ref["evaluator"]
    _assert_verified_prepare_task(captured["task"])
    assert json.loads(await runtime.store.get_text(captured["tree_ref"])) == (
        runtime.tree.to_dict()
    )
    await runtime.aclose()


@pytest.mark.asyncio
async def test_prepare_phase_reuses_frozen_evaluator_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}

    async def run_evaluator_plan(**kwargs):
        del kwargs
        raise AssertionError("evaluator must be skipped when a checkpoint exists")

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
        "athena.research.prepare.evaluator.run_evaluator_plan",
        run_evaluator_plan,
        raising=False,
    )
    monkeypatch.setattr(
        "athena.research.prepare.baseline.run_prepare_plan",
        run_prepare_plan,
        raising=False,
    )
    authority = _MemoryBaselineAuthorityStore()
    runtime = ResearchRuntime(
        project_root=tmp_path,
        task="predict survival",
        baseline_authority=authority,
    )
    runtime.register_supervisor(provider=object())
    await runtime.git.init()
    runtime.agents.start()
    frozen_dir = tmp_path / "frozen-evaluator"
    frozen_dir.mkdir()
    (frozen_dir / "README.md").write_text("# frozen\n", encoding="utf-8")
    (frozen_dir / "evaluate.py").write_text("# evaluator\n", encoding="utf-8")
    frozen_ref = await runtime.store.put_text(
        EvaluatorDescriptor(
            dir_path=str(frozen_dir),
            readme_ref=await runtime.store.put_text("# frozen\n"),
            entrypoint="evaluate.py",
        ).model_dump_json()
    )
    runtime.state.evaluator_ref = frozen_ref
    runtime.state.final_evaluator_ref = frozen_ref
    runtime.supervisor.evaluator_ref = frozen_ref
    runtime.supervisor.final_evaluator_ref = frozen_ref
    events: list[tuple[str, dict[str, object]]] = []
    runtime.subscribe(lambda kind, payload: events.append((kind, payload)))
    _install_verified_prepare_gate(monkeypatch)

    result = await runtime.services.workflow.phases.run_prepare_phase()

    assert result.evaluator_ref == frozen_ref
    assert any(
        kind == "output" and "reused SEARCH evaluator" in str(payload.get("text"))
        for kind, payload in events
    )
    assert runtime.state.evaluator_ref == frozen_ref
    _assert_verified_prepare_task(captured["task"])
    await runtime.aclose()


@pytest.mark.asyncio
async def test_default_validation_adapter_uses_frozen_inputs_and_supervisor_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}

    async def run_validation_plan(**kwargs):
        captured.update(kwargs)
        deps = kwargs["deps"]
        checkpoint_ref = await deps.store.put_text("validation checkpoint")
        await deps.checkpoint(checkpoint_ref)
        return ValidationResult(
            result_id=kwargs["input"].validation_key,
            status="COMPLETED",
            test_score=kwargs["input"].reference_metric,
            final_test_score=0.79,
            sota_commit=kwargs["input"].sota_commit,
            validation_commit=kwargs["input"].sota_commit,
            predictions_ref=await deps.store.put_text("final predictions"),
            evidence_ref=await deps.store.put_text("final evidence"),
        )

    monkeypatch.setattr(
        "athena.research.runtime.phase_runner.run_validation_plan",
        run_validation_plan,
        raising=False,
    )
    runtime = ResearchRuntime(project_root=tmp_path, direction="minimize")
    runtime.register_supervisor(provider=object())
    base_commit = await runtime.git.init()
    runtime.agents.start()
    evaluator_ref = await runtime.store.put_text(
        DataScriptBundle(
            bundle_id="frozen-evaluator", entrypoint="eval.py"
        ).model_dump_json()
    )
    runtime.supervisor.evaluator_ref = evaluator_ref
    evidence_ref = await runtime.store.put_text("baseline evidence")
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

    result = await runtime.services.workflow.phases.run_validation_phase(
        base_commit,
        0.82,
    )

    validation_input = captured["input"]
    validation_deps = captured["deps"]
    assert result.final_test_score == pytest.approx(0.79)
    assert validation_input.sota_commit == base_commit
    assert validation_input.reference_metric == pytest.approx(0.82)
    assert validation_input.direction == "minimize"
    assert validation_input.final_evaluator_ref == evaluator_ref
    assert validation_deps.workspace.branch == "athena/validate"
    assert callable(validation_deps.independent_review)
    assert runtime.state.validation == {
        "result_ref": await runtime.store.put_text("validation checkpoint")
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
    raw_ref = await runtime.store.put_text(full)

    await runtime.events.project_agent_event(
        "hyp_1",
        "command/stdout",
        "exec:run",
        {"delta": "ignored stream delta"},
    )
    await runtime.events.project_agent_event(
        "hyp_1",
        "command/stderr",
        "exec:run",
        {"delta": "ignored stream delta"},
    )
    await runtime.events.project_agent_event(
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
    redacted_full = await runtime.store.get_text(str(event["artifact_ref"]))
    assert secret not in redacted_full
    assert "[REDACTED]" in redacted_full
    assert "stdout-tail" in redacted_full
    assert "stderr-tail" in redacted_full
    with pytest.raises(ArtifactNotFoundError):
        await runtime.store.get_text(raw_ref)
    assert runtime.store.path_for(str(event["artifact_ref"])).is_file()
    await runtime.aclose()
