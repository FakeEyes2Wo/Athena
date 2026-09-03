"""Hermetic tests for the breakpoint-resume behavior added in this plan.

These tests stub out git/agent subprocesses so they run inside the sandbox;
the sandbox denies named pipes used by real ``git`` subprocess capture.
"""

import asyncio
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus
from athena.core.workspace import GitWorkBranch
from athena.research.prepare import orchestrator
from athena.research.prepare.authority import (
    BaselineAuthorityConflict,
    BaselineAuthorityError,
    PrepareAttestation,
    SealedBaseline,
    VerifiedBaselineBundle,
)
from athena.research.prepare.baseline_research import (
    BaselineResearchError,
    BaselineVerification,
    VerifiedBaseline,
    design_sha256,
    load_baseline_artifacts,
    research_sha256,
    verification_bytes,
    write_verification,
)
from athena.research.runtime.phase_runner import PhaseRunner
from athena.research.runtime import ResearchRuntime
from athena.research.contracts import EvaluatorDescriptor
from athena.research.runtime.control import (
    _consume_lifecycle_result,
    start as start_lifecycle,
)
from athena.research.runtime.resume_contract import ResearchControlError
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
                    "field": "labeled_samples",
                    "value": 480,
                    "evidence": {
                        "kind": "eda",
                        "reference": "EDA_HANDOFF.md#dataset-size",
                        "claim": "480 labeled training images",
                    },
                },
                {
                    "field": "group_count",
                    "value": 120,
                    "evidence": {
                        "kind": "eda",
                        "reference": "EDA_HANDOFF.md#groups",
                        "claim": "120 independent groups",
                    },
                },
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
        "limitations": ["The source data distribution differs from local data."],
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
    def __init__(
        self,
        sealed: SealedBaseline | None = None,
        *,
        load_error: Exception | None = None,
    ) -> None:
        self.sealed = sealed
        self.load_error = load_error

    async def load(self) -> SealedBaseline | None:
        if self.load_error is not None:
            raise self.load_error
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
        runtime.baseline_authority.sealed = SealedBaseline(
            generation=verified.authority_generation,
            bundle=_bundle_for_verified(verified),
        )
        return verified

    monkeypatch.setattr(orchestrator, "prepare_eda", asserted_eda)
    monkeypatch.setattr(orchestrator, "prepare_baseline_design", verified_design)


def _stub_runtime(tmp_path: Path, *, task_understanding=None) -> ResearchRuntime:
    runtime = ResearchRuntime(
        project_root=tmp_path,
        auto_confirm=True,
        task_confirmation_gate=False,
    )
    runtime.session.lifecycle.provider = object()
    runtime.session.lifecycle.task_text = "predict titanic survival"
    runtime.state.phase = "PREPARE"
    runtime.state.status = "RUNNING"
    runtime.state.task_understanding = task_understanding
    runtime.state.task_text = "predict titanic survival" if task_understanding else None
    runtime.state.handoff_refs = {}

    class FakeGit:
        async def init(self, *args, **kwargs):
            return None

    class FakeAgents:
        def start(self):
            return None

    runtime.services.infrastructure.git = FakeGit()
    runtime.services.infrastructure.agents = FakeAgents()
    return runtime


async def _failed_prepare_runtime(
    tmp_path: Path,
) -> tuple[ResearchRuntime, asyncio.Event]:
    restarted = asyncio.Event()
    calls = 0

    async def prepare() -> PrepareResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first PREPARE failed")
        restarted.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runtime = ResearchRuntime(
        project_root=tmp_path,
        prepare_phase=prepare,
        task_confirmation_gate=False,
        auto_confirm=True,
    )
    await runtime.start_task("predict churn")
    first = runtime.session.lifecycle.task
    assert first is not None
    with pytest.raises(RuntimeError, match="first PREPARE failed"):
        await first
    assert runtime.state.status == "FAILED"
    return runtime, restarted


def test_lifecycle_done_callback_observes_background_failure() -> None:
    observed: list[bool] = []
    task = SimpleNamespace(
        cancelled=lambda: False,
        exception=lambda: observed.append(True),
    )

    _consume_lifecycle_result(task)

    assert observed == [True]


@pytest.mark.asyncio
async def test_lifecycle_start_does_not_require_clarification_services() -> None:
    class FakeGit:
        async def init(self, *args, **kwargs) -> None:
            return None

    class FakeAgents:
        def start(self) -> None:
            return None

    class FakeSupervisor:
        async def start(self) -> None:
            return None

    state = SimpleNamespace(
        status="IDLE",
        task_text="confirmed task",
        task_understanding={"title": "confirmed task"},
        save=lambda _path: None,
    )
    lifecycle = SimpleNamespace(
        task=None,
        task_text="confirmed task",
        started=False,
    )
    runtime = SimpleNamespace(
        session=SimpleNamespace(lifecycle=lifecycle),
        task_text="confirmed task",
        state=state,
        state_path=Path("state.json"),
        git=FakeGit(),
        agents=FakeAgents(),
        supervisor=FakeSupervisor(),
        start_survey=lambda: None,
    )

    task = await start_lifecycle(runtime)
    await task

    assert lifecycle.started is True
    assert state.status == "RUNNING"


@pytest.mark.asyncio
async def test_start_task_persists_first_task_text_and_reuses_it(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = SimpleNamespace(done=lambda: False)

    status = await runtime.start_task("predict titanic survival")

    assert status == "RUNNING"
    assert runtime.task_text == "predict titanic survival"
    assert runtime.state.task_text == "predict titanic survival"
    assert runtime.state_path.is_file()
    assert runtime.state.task_understanding is not None

    runtime.state.task_understanding = {"title": "titanic"}
    await runtime.start_task("continue")

    assert runtime.task_text == "predict titanic survival"
    assert runtime.state.task_text == "predict titanic survival"


@pytest.mark.asyncio
async def test_start_task_keeps_task_text_when_understanding_turn_crashed(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    original_task = "https://www.kaggle.com/competitions/kaggriculture"
    runtime.state.task_text = original_task
    runtime.state.task_understanding = None
    runtime.session.lifecycle.task_text = original_task
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = SimpleNamespace(done=lambda: False)
    runtime.state.save(runtime.state_path)

    status = await runtime.start_task("continue")

    assert status == "RUNNING"
    assert runtime.task_text == original_task
    assert runtime.state.task_text == original_task
    assert runtime.state_path.is_file()


@pytest.mark.asyncio
async def test_start_skips_task_understanding_when_persisted(tmp_path: Path) -> None:
    runtime = _stub_runtime(
        tmp_path, task_understanding={"title": "titanic", "target": "survival"}
    )
    outputs: list[dict[str, object]] = []

    async def publish_output(**kwargs) -> None:
        outputs.append(kwargs)

    runtime.publish_output = publish_output  # type: ignore[method-assign]
    runtime.start_survey = lambda: None  # type: ignore[method-assign]

    class FakeAgentTurns:
        async def run_supervisor_turn(self, text):
            raise AssertionError("task understanding must be skipped")

    runtime.services.workflow.agent_turns = FakeAgentTurns()

    class FakeSupervisor:
        def __init__(self, state) -> None:
            self.state = state

        async def start(self):
            return None

    runtime.services.workflow.supervisor = FakeSupervisor(runtime.state)

    await runtime.start()

    assert runtime.session.lifecycle.started is True
    assert runtime.session.lifecycle.task is not None
    # The inline task-understanding turn is gone; confirmed state is immutable.
    assert not any("任务理解中" in str(output.get("text")) for output in outputs)
    runtime.session.lifecycle.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runtime.session.lifecycle.task


@pytest.mark.asyncio
async def test_run_prepare_phase_reuses_frozen_evaluator(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}
    roots = [
        tmp_path / "workspaces" / "evaluator" / "evaluate",
        tmp_path / "workspaces" / "final_evaluator" / "evaluate",
    ]
    for index, root in enumerate(roots):
        root.mkdir(parents=True)
        (root / "README.md").write_text("# frozen\n", encoding="utf-8")
        (root / "evaluate.py").write_text("# evaluator\n", encoding="utf-8")
        (root / "HANDOFF.md").write_text("# handoff\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            "[project]\nname='eval'\n", encoding="utf-8"
        )
        (root / "labels.csv").write_text(
            f"__athena_row_id,label\n{index},negative\n", encoding="utf-8"
        )
        (root / "metric.json").write_text(
            '{"contract_version":2,"task_id":"task",'
            '"task_type":"classification","primary_metric":"macro_f1",'
            '"class_labels":["negative","positive"],'
            '"prediction_file":"predictions__task.csv",'
            '"prediction_id_column":"__athena_row_id",'
            '"prediction_column":"prediction"}',
            encoding="utf-8",
        )
    descriptor = EvaluatorDescriptor(
        dir_path=str(roots[0]),
        readme_ref="sha256:" + "a" * 64,
        entrypoint="evaluate.py",
    ).model_dump_json()

    class FakeSupervisor:
        def __init__(self, frozen_ref: str) -> None:
            self._evaluator_ref = frozen_ref
            self._final_evaluator_ref = frozen_ref
            self.checked_refs: list[str] = []
            self.checked_final_refs: list[str] = []

        @property
        def evaluator_ref(self) -> str:
            return self._evaluator_ref

        @property
        def final_evaluator_ref(self) -> str:
            return self._final_evaluator_ref

        async def checkpoint_evaluator(self, ref: str) -> None:
            self.checked_refs.append(ref)

        async def checkpoint_final_evaluator(self, ref: str) -> None:
            self.checked_final_refs.append(ref)

    class FakeStore:
        async def get_text(self, ref):
            return descriptor

        async def put_text(self, text):
            return "tree-ref"

    class FakeBus:
        def project_agent_event(self, *args, **kwargs):
            return None

    class FakeGit:
        async def init(self, *args, **kwargs):
            return "base-commit"

        async def create(self, commit, branch, name=None):
            return SimpleNamespace(
                path=str(tmp_path / "workspaces" / "eda"),
                base_commit=commit,
            )

    async def publish_output(**_kwargs) -> None:
        return None

    frozen_ref = "sha256:" + "f" * 64
    authority = _MemoryBaselineAuthorityStore()
    state = SimpleNamespace(
        phase="PREPARE", status="RUNNING", eda_dir=None, save=lambda path: None
    )
    supervisor = FakeSupervisor(frozen_ref)
    rt = SimpleNamespace(
        prepare_phase=None,
        provider=object(),
        state=state,
        supervisor=supervisor,
        tree=SimpleNamespace(to_dict=dict),
        publish_output=publish_output,
        root=tmp_path,
        state_path=tmp_path / ".athena" / "state.json",
        workspaces_root=tmp_path / "workspaces",
        config=SimpleNamespace(
            dataset_path=None,
            target_column=None,
            split_seed=0,
            paths=SimpleNamespace(athena=tmp_path / ".athena"),
        ),
        task_confirmation_gate=False,
        git=FakeGit(),
        store=FakeStore(),
        events=FakeBus(),
        registry=SimpleNamespace(contains=lambda name: True),
        agents=SimpleNamespace(reap=lambda agent_id: None),
        execution=object(),
        evaluator=object(),
        baseline_authority=authority,
        task_text="predict survival",
        kaggle_tools=lambda agent_type: None,
        ideator_tools=lambda: None,
    )

    async def run_evaluator_plan(**kwargs):
        del kwargs
        raise AssertionError("evaluator must be skipped when a checkpoint exists")

    async def run_prepare_plan(**kwargs):
        captured.update(kwargs)
        return PrepareResult(
            evaluator_ref=kwargs["evaluator_ref"],
            metric=0.71,
            commit=kwargs["workspace"].base_commit,
            predictions_ref="pred-ref",
            evidence_ref="evidence-ref",
            report_ref="report-ref",
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

    async def fake_handoff(
        self, agent_id, agent_type, workspace, output_file, content, *, reap_after=False
    ):
        del agent_id, agent_type, content, reap_after
        Path(workspace).mkdir(parents=True, exist_ok=True)
        (Path(workspace) / output_file).write_text("stub\n", encoding="utf-8")

    monkeypatch.setattr(PhaseRunner, "_run_handoff_agent", fake_handoff)
    _install_verified_prepare_gate(monkeypatch)

    result = await PhaseRunner(rt).run_prepare_phase()

    assert result.evaluator_ref == frozen_ref
    assert rt.supervisor.checked_refs == []
    for filename in (
        "BASELINE_RESEARCH.json",
        "BASELINE_RESEARCH_VERIFICATION.json",
        "BASELINE_DESIGN.md",
    ):
        assert filename in str(captured["task"])
    assert "Selected candidate: resnet-transfer" in str(captured["task"])
    assert "Training strategy: partial_finetune" in str(captured["task"])


async def _seed_attested_prepare_checkpoint(
    tmp_path: Path,
    authority: _MemoryBaselineAuthorityStore,
) -> tuple[str, str, str]:
    workspace_root = tmp_path / "workspaces" / "eda"
    verified = _write_verified_baseline_fixture(workspace_root)
    runtime = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)
    evaluator_ref = await runtime.store.put_text('{"frozen":true}')
    baseline_commit = "b" * 40
    evidence_ref = await runtime.store.put_text(
        json.dumps(
            {
                "plan": "prepare",
                "metric": 0.71,
                "commit": baseline_commit,
                "predictions_ref": "sha256:" + "1" * 64,
                "metrics_ref": None,
                "report_ref": "sha256:" + "2" * 64,
                "exploration_ref": None,
                "outputs": {
                    "predictions": "outputs/predictions",
                    "report": "outputs/report.md",
                },
            },
            ensure_ascii=False,
        )
    )
    attestation = PrepareAttestation(
        research_sha256=verified.verification.research_sha256,
        design_sha256=verified.verification.design_sha256,
        baseline_commit=baseline_commit,
        evaluator_ref=evaluator_ref,
        evidence_ref=evidence_ref,
    )
    authority.sealed = SealedBaseline(
        generation=1,
        bundle=_bundle_for_verified(verified),
        attestation=attestation,
    )

    runtime.state.phase = "PREPARE"
    runtime.state.status = "RUNNING"
    runtime.state.eda_dir = "workspaces/eda"
    handoff_text = "# Confirmed task\n\nImprove the trusted baseline.\n"
    handoff_ref = await runtime.store.put_text(handoff_text)
    runtime.state.task_text = "Improve the trusted baseline."
    runtime.state.task_understanding = {"goal": "Improve the trusted baseline."}
    runtime.state.handoff_refs["task_clarification"] = handoff_ref
    handoff_path = runtime.config.paths.handoffs / "TASK_CLARIFICATION.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(handoff_text, encoding="utf-8")
    runtime.tree.add_hypothesis(
        Hypothesis(
            id="baseline",
            statement="trusted PREPARE baseline",
            intervention="establish the baseline implementation",
            expected_effect="provide the SEARCH reference metric",
        )
    )
    runtime.tree.add_experiment(
        "exp_baseline",
        Experiment(
            hypothesis_id="baseline",
            commit=baseline_commit,
            plan=ExperimentPlan(
                kind="baseline",
                change="prepare trusted baseline",
                run_config_ref=evaluator_ref,
                budget={},
                acceptance_rule="trusted evaluator score",
            ),
            gitwork=GitWorkBranch(
                path=str(tmp_path),
                branch="main",
                base_commit=baseline_commit,
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_baseline",
                primary=0.71,
                per_sample=evidence_ref,
            ),
        ),
    )
    runtime.tree.set_sota("exp_baseline")
    runtime.tree.save(runtime.config.paths.tree)
    runtime.state.save(runtime.state_path)
    return evaluator_ref, evidence_ref, baseline_commit


@pytest.mark.asyncio
async def test_fresh_runtime_skips_prepare_for_exact_external_attestation(
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    await _seed_attested_prepare_checkpoint(tmp_path, authority)
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)

    await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "SEARCH"
    assert restarted.state.status == "RUNNING"


@pytest.mark.asyncio
@pytest.mark.parametrize("state_ref_kind", ["conflicting", "empty"])
async def test_attested_resume_restores_evaluator_used_by_search_plan(
    state_ref_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    evaluator_ref, _evidence_ref, baseline_commit = (
        await _seed_attested_prepare_checkpoint(tmp_path, authority)
    )
    checkpoint = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)
    checkpoint.state.evaluator_ref = (
        await checkpoint.store.put_text('{"forged":true}')
        if state_ref_kind == "conflicting"
        else None
    )
    checkpoint.state.save(checkpoint.state_path)
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)

    await restarted.supervisor._phases._run_prepare()
    assert restarted.state.evaluator_ref == evaluator_ref
    restarted.tree.add_hypothesis(
        Hypothesis(
            id="hyp_search",
            parent_id="exp_baseline",
            statement="test the next change",
            intervention="change model.py",
            expected_effect="improve the trusted metric",
        )
    )

    async def create_workspace(commit: str, branch: str, name=None):
        del name
        return GitWorkBranch(
            path=str(tmp_path / "workspaces" / branch),
            branch=branch,
            base_commit=commit,
        )

    async def resume_agent(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        restarted.supervisor._deps.runtime.workspaces,
        "create",
        create_workspace,
    )
    monkeypatch.setattr(
        restarted.supervisor._deps.runtime.agents,
        "resume_agent",
        resume_agent,
    )

    await restarted.supervisor.start_plan("hyp_search")
    plan_input = await restarted.supervisor.plan_input("hyp_search")

    assert restarted.state.phase == "SEARCH"
    assert restarted.state.evaluator_ref == evaluator_ref
    assert plan_input.evaluator_ref == evaluator_ref
    assert plan_input.reference_experiment_id == "exp_baseline"
    assert plan_input.reference_metric == 0.71
    assert restarted.tree.get_experiment("exp_baseline").commit == baseline_commit


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["commit", "evaluator_ref", "evidence_ref"])
async def test_fresh_runtime_rejects_attestation_mismatching_local_tree(
    difference: str,
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    await _seed_attested_prepare_checkpoint(tmp_path, authority)
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)
    experiment = restarted.tree.get_experiment("exp_baseline")
    if difference == "commit":
        experiment.commit = "c" * 40
    elif difference == "evaluator_ref":
        experiment.plan.run_config_ref = "sha256:" + "c" * 64
    else:
        assert difference == "evidence_ref"
        assert experiment.eval is not None
        experiment.eval.per_sample = "sha256:" + "c" * 64

    assert await PhaseRunner(restarted).baseline_resume_is_attested() is False

    with pytest.raises(BaselineAuthorityError, match="not attested"):
        await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "PREPARE"


@pytest.mark.asyncio
async def test_fresh_runtime_rejects_tampered_baseline_metric(tmp_path: Path) -> None:
    authority = _MemoryBaselineAuthorityStore()
    await _seed_attested_prepare_checkpoint(tmp_path, authority)
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)
    experiment = restarted.tree.get_experiment("exp_baseline")
    assert experiment.eval is not None
    experiment.eval.primary = 0.99

    assert await PhaseRunner(restarted).baseline_resume_is_attested() is False
    with pytest.raises(BaselineAuthorityError, match="not attested"):
        await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "PREPARE"


def _replace_attested_evidence(
    authority: _MemoryBaselineAuthorityStore,
    experiment: Experiment,
    evidence_ref: str,
) -> None:
    assert authority.sealed is not None
    assert authority.sealed.attestation is not None
    original = authority.sealed.attestation
    authority.sealed = SealedBaseline(
        generation=1,
        bundle=authority.sealed.bundle,
        attestation=PrepareAttestation(
            research_sha256=original.research_sha256,
            design_sha256=original.design_sha256,
            baseline_commit=original.baseline_commit,
            evaluator_ref=original.evaluator_ref,
            evidence_ref=evidence_ref,
        ),
    )
    assert experiment.eval is not None
    experiment.eval.per_sample = evidence_ref


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing_artifact",
        "digest_mismatch",
        "invalid_json",
        "missing_metric",
        "missing_commit",
    ],
)
async def test_fresh_runtime_rejects_invalid_attested_score_evidence(
    failure: str,
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    _evaluator_ref, _evidence_ref, baseline_commit = (
        await _seed_attested_prepare_checkpoint(tmp_path, authority)
    )
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)
    if failure == "missing_artifact":
        evidence_ref = "sha256:" + "9" * 64
    elif failure == "invalid_json":
        evidence_ref = await restarted.store.put_text("{")
    else:
        evidence = {"plan": "prepare", "metric": 0.71, "commit": baseline_commit}
        if failure == "missing_metric":
            evidence.pop("metric")
        elif failure == "missing_commit":
            evidence.pop("commit")
        evidence_ref = await restarted.store.put_text(json.dumps(evidence))
        if failure == "digest_mismatch":
            restarted.store.path_for(evidence_ref).write_text(
                '{"plan":"prepare","metric":0.99,"commit":"tampered"}',
                encoding="utf-8",
            )
    experiment = restarted.tree.get_experiment("exp_baseline")
    _replace_attested_evidence(authority, experiment, evidence_ref)

    with pytest.raises(BaselineAuthorityError, match="trusted PREPARE evidence"):
        await PhaseRunner(restarted).baseline_resume_is_attested()
    with pytest.raises(BaselineAuthorityError, match="trusted PREPARE evidence"):
        await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "PREPARE"


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["metric", "commit"])
async def test_fresh_runtime_rejects_attested_score_content_mismatch(
    difference: str,
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    _evaluator_ref, _evidence_ref, baseline_commit = (
        await _seed_attested_prepare_checkpoint(tmp_path, authority)
    )
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)
    evidence = {
        "plan": "prepare",
        "metric": 0.99 if difference == "metric" else 0.71,
        "commit": "c" * 40 if difference == "commit" else baseline_commit,
    }
    evidence_ref = await restarted.store.put_text(json.dumps(evidence))
    experiment = restarted.tree.get_experiment("exp_baseline")
    _replace_attested_evidence(authority, experiment, evidence_ref)

    assert await PhaseRunner(restarted).baseline_resume_is_attested() is False
    with pytest.raises(BaselineAuthorityError, match="not attested"):
        await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "PREPARE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority_state",
    ["missing_capability", "missing_record", "missing_attestation", "wrong_generation"],
)
async def test_fresh_runtime_rejects_missing_or_wrong_authority_state(
    authority_state: str,
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    await _seed_attested_prepare_checkpoint(tmp_path, authority)
    assert authority.sealed is not None
    if authority_state == "missing_capability":
        restarted = ResearchRuntime(project_root=tmp_path)
    else:
        if authority_state == "missing_record":
            authority.sealed = None
        elif authority_state == "missing_attestation":
            authority.sealed = SealedBaseline(
                generation=0,
                bundle=authority.sealed.bundle,
            )
        else:
            assert authority_state == "wrong_generation"
            authority.sealed = SealedBaseline(
                generation=2,
                bundle=authority.sealed.bundle,
                attestation=authority.sealed.attestation,
            )
        restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)

    runner = PhaseRunner(restarted)
    if authority_state in {"missing_capability", "wrong_generation"}:
        expected_message = (
            "requires" if authority_state == "missing_capability" else "lifecycle"
        )
        with pytest.raises(BaselineAuthorityError, match=expected_message):
            await runner.baseline_resume_is_attested()
    else:
        assert await runner.baseline_resume_is_attested() is False

    with pytest.raises(BaselineAuthorityError):
        await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "PREPARE"


@pytest.mark.asyncio
async def test_fresh_runtime_rejects_authority_outage(tmp_path: Path) -> None:
    authority = _MemoryBaselineAuthorityStore()
    await _seed_attested_prepare_checkpoint(tmp_path, authority)
    authority.load_error = OSError("authority offline")
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)

    with pytest.raises(BaselineAuthorityError, match="load failed"):
        await PhaseRunner(restarted).baseline_resume_is_attested()

    with pytest.raises(BaselineAuthorityError, match="load failed"):
        await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "PREPARE"


@pytest.mark.asyncio
async def test_fresh_runtime_rejects_mutated_baseline_artifact(tmp_path: Path) -> None:
    authority = _MemoryBaselineAuthorityStore()
    await _seed_attested_prepare_checkpoint(tmp_path, authority)
    (tmp_path / "workspaces" / "eda" / "BASELINE_DESIGN.md").write_text(
        "Selected candidate: `resnet-transfer`\nTraining strategy: `classical`\n",
        encoding="utf-8",
    )
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)

    with pytest.raises(BaselineResearchError, match="BASELINE_DESIGN.md"):
        await PhaseRunner(restarted).baseline_resume_is_attested()

    with pytest.raises(BaselineResearchError, match="BASELINE_DESIGN.md"):
        await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "PREPARE"


@pytest.mark.asyncio
async def test_run_general_turn_persists_agent_id_before_wait(
    tmp_path: Path, monkeypatch
) -> None:
    from athena.agents.task_agents import GeneralResult
    from athena.research.turns import runner as atr

    saves: list[str] = []
    state = SimpleNamespace(
        phase="PREPARE",
        status="RUNNING",
        task_research_task=None,
        task_research_ref=None,
        task_research_agent_id=None,
        save=lambda path: saves.append(str(path)),
    )

    class FakeAgents:
        async def create_root(self, agent_type, request, *, name, agent_id=None):
            del name, agent_id
            return "general-worker", "run-1"

    rt = SimpleNamespace(
        state=state,
        provider=object(),
        registry=SimpleNamespace(contains=lambda name: True),
        store=object(),
        execution=object(),
        agents=FakeAgents(),
        events=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        root=tmp_path,
        state_path=tmp_path / ".athena" / "state.json",
        kaggle_tools=lambda kind: None,
    )

    async def wait_run_events(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace()

    monkeypatch.setattr(
        "athena.research.turns.common.wait_run_events",
        wait_run_events,
        raising=False,
    )

    async def load_agent_result(summary, store, schema):
        del summary, store, schema
        return GeneralResult(result="done", files=["summary.md"])

    monkeypatch.setattr(
        "athena.research.turns.general.load_agent_result",
        load_agent_result,
        raising=False,
    )

    outcome = await atr.AgentTurnRunner(rt).run_general_turn("inspect competition")

    assert outcome.agent_id == "general-worker"
    assert outcome.result == {"result": "done", "files": ["summary.md"]}
    assert state.task_research_task == "inspect competition"
    assert state.task_research_agent_id == "general-worker"
    assert saves == [str(rt.state_path)]


@pytest.mark.asyncio
async def test_run_general_turn_interrupts_worker_on_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    from athena.research.turns import runner as atr

    state = SimpleNamespace(
        phase="PREPARE",
        status="RUNNING",
        task_research_task=None,
        task_research_ref=None,
        task_research_agent_id=None,
        save=lambda path: None,
    )
    interrupted: list[tuple[str, str]] = []

    class FakeAgents:
        async def create_root(self, agent_type, request, *, name, agent_id=None):
            del name, agent_id
            return "general-worker", "run-1"

        async def interrupt(self, agent_id, reason):
            interrupted.append((agent_id, reason))

    rt = SimpleNamespace(
        state=state,
        provider=object(),
        registry=SimpleNamespace(contains=lambda name: True),
        store=object(),
        execution=object(),
        agents=FakeAgents(),
        events=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        root=tmp_path,
        state_path=tmp_path / ".athena" / "state.json",
        kaggle_tools=lambda kind: None,
    )
    monkeypatch.setattr("athena.research.turns.common.AGENT_TURN_TIMEOUT_SECONDS", 0)

    async def never_finishes(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(10)

    monkeypatch.setattr(
        "athena.research.turns.common.wait_run_events",
        never_finishes,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="timed out"):
        await atr.AgentTurnRunner(rt).run_general_turn("inspect competition")

    assert interrupted == [("general-worker", "general_turn_timeout")]


@pytest.mark.asyncio
async def test_start_task_preserves_reconstructed_task_text_from_legacy_understanding(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime.state.task_text = None
    runtime.state.task_understanding = {
        "title": "Kaggriculture farming simulation",
        "dataset": "kaggriculture environment",
        "target": "maximize income",
    }
    reconstructed = (
        "Kaggriculture farming simulation kaggriculture environment maximize income"
    )
    runtime.session.lifecycle.task_text = reconstructed
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = SimpleNamespace(done=lambda: False)

    status = await runtime.start_task("continue")

    assert status == "RUNNING"
    assert runtime.task_text == reconstructed
    assert runtime.state.task_text is None


@pytest.mark.asyncio
async def test_failed_legacy_understanding_reconstructs_task_text_for_replacement(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime.state.status = "FAILED"
    runtime.state.task_text = None
    runtime.state.task_understanding = {
        "title": "Kaggriculture farming simulation",
        "dataset": "kaggriculture environment",
        "target": "maximize income",
    }
    runtime.session.lifecycle.task_text = "stale process-local text"
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = None
    replacement_hold = asyncio.Event()
    replacements: list[asyncio.Task[None]] = []

    class FakeSupervisor:
        async def resume(self, *, restarting: bool = False) -> str:
            assert restarting is True
            runtime.state.status = "RUNNING"
            return "RUNNING"

    runtime.services.workflow.supervisor = FakeSupervisor()

    async def fake_start() -> asyncio.Task[None]:
        replacement = asyncio.create_task(replacement_hold.wait())
        replacements.append(replacement)
        runtime.session.lifecycle.task = replacement
        return replacement

    runtime.start = fake_start  # type: ignore[method-assign]
    try:
        assert await runtime.resume_current_task() == "RUNNING"
        assert runtime.session.lifecycle.task_text == (
            "Kaggriculture farming simulation kaggriculture environment maximize income"
        )
        assert runtime.state.task_text is None
        assert len(replacements) == 1
        assert runtime.session.lifecycle.task is replacements[0]
    finally:
        replacement_hold.set()
        await asyncio.gather(*replacements, return_exceptions=True)


@pytest.mark.asyncio
async def test_resume_restarts_a_rebuilt_runtime_for_a_persisted_prepare_run(
    tmp_path: Path,
) -> None:
    """会话切换后 runtime 是新建的（``_started=False``）：``/resume`` 必须真的重进阶段机。

    GUI 切走会话时 supervisor 被 suspend + aclose，切回来拿到的是一个全新的
    runtime。旧逻辑靠 ``_started`` 判断"这次是续跑"，新 runtime 一律落到
    ``ensure_started``，而它在没有可信 baseline 时不启动，于是 PREPARE 停在
    "status=RUNNING 但没有任何协程在跑"的悬空态——正是用户点"继续"没反应的原因。
    """
    runtime = _stub_runtime(tmp_path)
    runtime.state.phase = "PREPARE"
    runtime.state.status = "WAITING"
    runtime.state.task_text = "persisted prepare task"
    runtime.state.save(runtime.state_path)
    runtime.session.lifecycle.started = False
    runtime.session.lifecycle.task = None
    started: list[bool] = []
    resumed: list[bool] = []

    class FakeTree:
        def best_experiment_id(self):
            return None

    class FakeSupervisor:
        def __init__(self, state) -> None:
            self.state = state
            self.tree = FakeTree()

        def is_stopped(self) -> bool:
            return False

        async def resume(self, *, restarting: bool = False) -> str:
            resumed.append(restarting)
            self.state.status = "RUNNING"
            return self.state.status

    runtime.services.workflow.supervisor = FakeSupervisor(runtime.state)

    async def start():
        started.append(True)

    runtime.start = start

    assert await runtime.message("/resume") == "RUNNING"
    assert started == [True], "PREPARE 续跑必须重新进入阶段机"
    assert resumed == [True], "restarting=True 才不会重复 spawn SEARCH 调度器"


@pytest.mark.asyncio
async def test_resume_does_not_start_a_project_without_durable_state(
    tmp_path: Path,
) -> None:
    """全新项目还没落过盘：``/resume`` 不得凭空把它推进阶段机。"""
    runtime = _stub_runtime(tmp_path)
    runtime.state.status = "WAITING"
    runtime.session.lifecycle.started = False
    runtime.session.lifecycle.task = None
    started: list[bool] = []

    class FakeTree:
        def best_experiment_id(self):
            return None

    class FakeSupervisor:
        def __init__(self, state) -> None:
            self.state = state
            self.tree = FakeTree()

        def is_stopped(self) -> bool:
            return False

        async def resume(self, *, restarting: bool = False) -> str:
            self.state.status = "RUNNING"
            return self.state.status

    runtime.services.workflow.supervisor = FakeSupervisor(runtime.state)

    async def start():
        started.append(True)

    runtime.start = start

    with pytest.raises(ResearchControlError) as caught:
        await runtime.message("/resume")

    assert caught.value.code == "resume_unavailable"
    assert started == []


@pytest.mark.asyncio
async def test_plain_continue_restarts_failed_current_task_without_reseeding(
    tmp_path: Path,
) -> None:
    runtime, restarted = await _failed_prepare_runtime(tmp_path)
    original_task = runtime.state.task_text
    original_understanding = dict(runtime.state.task_understanding or {})
    try:
        assert await runtime.message("  Continue  ") == "RUNNING"
        await asyncio.wait_for(restarted.wait(), timeout=1)

        assert runtime.state.task_text == original_task
        assert runtime.state.task_understanding == original_understanding
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_reloaded_idle_prepare_checkpoint_starts_a_new_lifecycle(
    tmp_path: Path,
) -> None:
    failed, _restarted = await _failed_prepare_runtime(tmp_path)
    first = failed.session.lifecycle.task
    await failed.aclose()

    entered = asyncio.Event()

    async def prepare() -> PrepareResult:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runtime = ResearchRuntime(
        project_root=tmp_path,
        prepare_phase=prepare,
        task_confirmation_gate=False,
        auto_confirm=True,
    )
    try:
        assert runtime.state.status == "IDLE"
        assert runtime.state.phase == "PREPARE"

        assert await runtime.resume_current_task() == "RUNNING"
        await asyncio.wait_for(entered.wait(), timeout=1)

        assert runtime.session.lifecycle.task is not first
        assert runtime.session.lifecycle.task is not None
        assert not runtime.session.lifecycle.task.done()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_repeated_continue_is_idempotent_while_restarted_task_is_live(
    tmp_path: Path,
) -> None:
    runtime, restarted = await _failed_prepare_runtime(tmp_path)
    try:
        assert await runtime.message("continue") == "RUNNING"
        await asyncio.wait_for(restarted.wait(), timeout=1)
        replacement = runtime.session.lifecycle.task
        runtime.session.lifecycle.task_text = "preserve process-local task text"
        task_text_before = runtime.session.lifecycle.task_text

        assert await runtime.message("continue") == "RUNNING"
        assert runtime.session.lifecycle.task_text == task_text_before
        assert runtime.session.lifecycle.task is replacement
        assert replacement is not None
        assert not replacement.done()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_waiting_live_lifecycle_resumes_without_spawning_replacement(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path, task_understanding={"title": "original task"})
    runtime.state.status = "WAITING"
    hold = asyncio.Event()
    live_task = asyncio.create_task(hold.wait())
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = live_task
    resume_calls: list[bool] = []

    class FakeSupervisor:
        async def resume(self, *, restarting: bool = False) -> str:
            resume_calls.append(restarting)
            runtime.state.status = "RUNNING"
            return "RUNNING"

    runtime.services.workflow.supervisor = FakeSupervisor()

    async def exploding_start() -> asyncio.Task[None]:
        raise AssertionError("live lifecycle must not be replaced")

    runtime.start = exploding_start  # type: ignore[method-assign]
    try:
        assert await runtime.resume_current_task() == "RUNNING"
        assert resume_calls == [False]
        assert runtime.session.lifecycle.task is live_task
    finally:
        hold.set()
        await live_task


@pytest.mark.asyncio
async def test_concurrent_resume_calls_create_one_lifecycle_task(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path, task_understanding={"title": "original task"})
    runtime.state.status = "FAILED"
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = None
    first_resume_entered = asyncio.Event()
    release_first_resume = asyncio.Event()
    replacement_hold = asyncio.Event()
    resume_calls: list[bool] = []
    started_tasks: list[asyncio.Task[None]] = []

    class FakeSupervisor:
        async def resume(self, *, restarting: bool = False) -> str:
            resume_calls.append(restarting)
            if len(resume_calls) == 1:
                first_resume_entered.set()
                await release_first_resume.wait()
            runtime.state.status = "RUNNING"
            return "RUNNING"

    runtime.services.workflow.supervisor = FakeSupervisor()

    async def fake_start() -> asyncio.Task[None]:
        task = asyncio.create_task(replacement_hold.wait())
        started_tasks.append(task)
        runtime.session.lifecycle.task = task
        runtime.session.lifecycle.started = True
        return task

    runtime.start = fake_start  # type: ignore[method-assign]
    first_resume = asyncio.create_task(runtime.resume_current_task())
    await asyncio.wait_for(first_resume_entered.wait(), timeout=1)
    second_started = asyncio.Event()

    async def second_call() -> str:
        second_started.set()
        return await runtime.resume_current_task()

    second_resume = asyncio.create_task(second_call())
    await second_started.wait()
    try:
        assert resume_calls == [True]
        assert second_resume.done() is False

        release_first_resume.set()
        assert await asyncio.gather(first_resume, second_resume) == [
            "RUNNING",
            "RUNNING",
        ]
        assert resume_calls == [True]
        assert len(started_tasks) == 1
        assert runtime.session.lifecycle.task is started_tasks[0]
    finally:
        release_first_resume.set()
        replacement_hold.set()
        await asyncio.gather(
            first_resume, second_resume, *started_tasks, return_exceptions=True
        )


@pytest.mark.asyncio
async def test_failed_resume_waits_for_old_exception_to_finish_unwinding(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path, task_understanding={"title": "original task"})
    runtime.state.status = "FAILED"
    runtime.session.lifecycle.started = True
    unwinding = asyncio.Event()
    release_unwind = asyncio.Event()
    replacement_hold = asyncio.Event()
    resume_calls: list[bool] = []
    started_tasks: list[asyncio.Task[None]] = []

    async def failing_lifecycle() -> None:
        try:
            raise RuntimeError("old PREPARE failure")
        finally:
            unwinding.set()
            await release_unwind.wait()

    old_task = asyncio.create_task(failing_lifecycle())
    runtime.session.lifecycle.task = old_task
    await unwinding.wait()

    class FakeSupervisor:
        async def resume(self, *, restarting: bool = False) -> str:
            resume_calls.append(restarting)
            runtime.state.status = "RUNNING"
            return "RUNNING"

    runtime.services.workflow.supervisor = FakeSupervisor()

    async def fake_start() -> asyncio.Task[None]:
        task = asyncio.create_task(replacement_hold.wait())
        started_tasks.append(task)
        runtime.session.lifecycle.task = task
        return task

    runtime.start = fake_start  # type: ignore[method-assign]
    resume_started = asyncio.Event()

    async def resume_call() -> str:
        resume_started.set()
        return await runtime.resume_current_task()

    resumed = asyncio.create_task(resume_call())
    await resume_started.wait()
    try:
        assert resumed.done() is False
        assert resume_calls == []

        release_unwind.set()
        assert await resumed == "RUNNING"
        assert old_task.done()
        assert resume_calls == [True]
        assert len(started_tasks) == 1
        assert runtime.session.lifecycle.task is started_tasks[0]
    finally:
        release_unwind.set()
        replacement_hold.set()
        await asyncio.gather(old_task, resumed, *started_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "phase", "has_task"),
    [
        ("STOPPED", "PREPARE", True),
        ("COMPLETED", "COMPLETED", True),
        ("IDLE", "PREPARE", False),
    ],
)
async def test_unavailable_continue_is_typed_before_infrastructure_actions(
    status: str,
    phase: str,
    has_task: bool,
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime.state.status = status
    runtime.state.phase = phase
    runtime.state.task_text = "original task" if has_task else None
    runtime.state.task_understanding = {"title": "original task"} if has_task else None
    runtime.session.lifecycle.started = False
    runtime.session.lifecycle.task = None

    class ExplodingSupervisor:
        def __getattribute__(self, name: str):
            raise AssertionError(f"Supervisor infrastructure touched: {name}")

    runtime.services.workflow.supervisor = ExplodingSupervisor()

    async def exploding_start() -> asyncio.Task[None]:
        raise AssertionError("runtime infrastructure started")

    runtime.start = exploding_start  # type: ignore[method-assign]

    with pytest.raises(ResearchControlError) as caught:
        await runtime.message("continue")

    assert caught.value.code == "resume_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guidance", ["continue research", "continue three more attempts"]
)
async def test_multiword_continue_guidance_reaches_supervisor_unchanged(
    guidance: str,
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path, task_understanding={"title": "original task"})
    received: list[str] = []

    class FakeSupervisor:
        async def message(self, text: str) -> str:
            received.append(text)
            return "guidance accepted"

    runtime.services.workflow.supervisor = FakeSupervisor()

    assert await runtime.message(guidance) == "guidance accepted"
    assert received == [guidance]


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_recovers", [True, False])
async def test_authoritative_prepare_resume_reuses_attested_baseline(
    authority_recovers: bool,
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    await _seed_attested_prepare_checkpoint(tmp_path, authority)
    baseline_root = tmp_path / "workspaces" / "eda"
    baseline_before = {
        path.relative_to(baseline_root): path.read_bytes()
        for path in baseline_root.rglob("*")
        if path.is_file()
    }
    authority.load_error = OSError("authority offline")
    runtime = ResearchRuntime(
        project_root=tmp_path,
        baseline_authority=authority,
        search_limit=0,
    )
    runtime.clarification_path.write_bytes(b"frozen confirmed clarification\n")
    original_task = runtime.state.task_text
    original_understanding = dict(runtime.state.task_understanding or {})
    original_handoffs = dict(runtime.state.handoff_refs)
    original_draft = runtime.clarification_path.read_bytes()
    original_handoff = runtime.handoffs_path.joinpath(
        "TASK_CLARIFICATION.md"
    ).read_bytes()

    class ExplodingClarification:
        async def start_or_resume(self, _task: str) -> object:
            raise AssertionError("resume must not enter clarification")

    runtime.services.workflow.clarification = ExplodingClarification()
    try:
        assert await runtime.resume_current_task() == "RUNNING"
        first = runtime.session.lifecycle.task
        assert first is not None
        with pytest.raises(BaselineAuthorityError, match="load failed"):
            await first
        assert runtime.state.status == "FAILED"

        if authority_recovers:
            authority.load_error = None

        assert await runtime.resume_current_task() == "RUNNING"
        replacement = runtime.session.lifecycle.task
        assert replacement is not None
        assert replacement is not first
        if authority_recovers:
            await asyncio.wait_for(replacement, timeout=1)
        else:
            with pytest.raises(BaselineAuthorityError, match="load failed"):
                await replacement

        assert runtime.state.task_text == original_task
        assert runtime.state.task_understanding == original_understanding
        assert runtime.state.handoff_refs == original_handoffs
        assert runtime.clarification_path.read_bytes() == original_draft
        assert (
            runtime.handoffs_path.joinpath("TASK_CLARIFICATION.md").read_bytes()
            == original_handoff
        )
        assert {
            path.relative_to(baseline_root): path.read_bytes()
            for path in baseline_root.rglob("*")
            if path.is_file()
        } == baseline_before
    finally:
        await runtime.aclose()
