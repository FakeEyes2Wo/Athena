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
from athena.research.runtime.control import start as start_lifecycle
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
    runtime.state.task_text = "https://www.kaggle.com/competitions/kaggriculture"
    runtime.state.task_understanding = None
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = SimpleNamespace(done=lambda: False)

    status = await runtime.start_task("continue")

    assert status == "RUNNING"
    assert runtime.task_text == "https://www.kaggle.com/competitions/kaggriculture"
    assert (
        runtime.state.task_text == "https://www.kaggle.com/competitions/kaggriculture"
    )
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
            return '{"frozen": true}'

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


def _seed_attested_prepare_checkpoint(
    tmp_path: Path,
    authority: _MemoryBaselineAuthorityStore,
) -> None:
    workspace_root = tmp_path / "workspaces" / "eda"
    verified = _write_verified_baseline_fixture(workspace_root)
    attestation = PrepareAttestation(
        research_sha256=verified.verification.research_sha256,
        design_sha256=verified.verification.design_sha256,
        baseline_commit="b" * 40,
        evaluator_ref="sha256:" + "e" * 64,
        evidence_ref="sha256:" + "d" * 64,
    )
    authority.sealed = SealedBaseline(
        generation=1,
        bundle=_bundle_for_verified(verified),
        attestation=attestation,
    )

    runtime = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)
    runtime.state.phase = "PREPARE"
    runtime.state.status = "RUNNING"
    runtime.state.eda_dir = "workspaces/eda"
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
            commit="b" * 40,
            plan=ExperimentPlan(
                kind="baseline",
                change="prepare trusted baseline",
                run_config_ref="sha256:" + "e" * 64,
                budget={},
                acceptance_rule="trusted evaluator score",
            ),
            gitwork=GitWorkBranch(
                path=str(tmp_path),
                branch="main",
                base_commit="b" * 40,
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_baseline",
                primary=0.71,
                per_sample="sha256:" + "d" * 64,
            ),
        ),
    )
    runtime.tree.set_sota("exp_baseline")
    runtime.tree.save(runtime.config.paths.tree)
    runtime.state.save(runtime.state_path)


@pytest.mark.asyncio
async def test_fresh_runtime_skips_prepare_for_exact_external_attestation(
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    _seed_attested_prepare_checkpoint(tmp_path, authority)
    restarted = ResearchRuntime(project_root=tmp_path, baseline_authority=authority)

    await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "SEARCH"
    assert restarted.state.status == "RUNNING"


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["commit", "evaluator_ref", "evidence_ref"])
async def test_fresh_runtime_rejects_attestation_mismatching_local_tree(
    difference: str,
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    _seed_attested_prepare_checkpoint(tmp_path, authority)
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
@pytest.mark.parametrize(
    "authority_state",
    ["missing_capability", "missing_record", "missing_attestation", "wrong_generation"],
)
async def test_fresh_runtime_rejects_missing_or_wrong_authority_state(
    authority_state: str,
    tmp_path: Path,
) -> None:
    authority = _MemoryBaselineAuthorityStore()
    _seed_attested_prepare_checkpoint(tmp_path, authority)
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
    if authority_state == "missing_capability":
        with pytest.raises(BaselineAuthorityError, match="requires"):
            await runner.baseline_resume_is_attested()
    else:
        assert await runner.baseline_resume_is_attested() is False

    with pytest.raises(BaselineAuthorityError):
        await restarted.supervisor._phases._run_prepare()

    assert restarted.state.phase == "PREPARE"


@pytest.mark.asyncio
async def test_fresh_runtime_rejects_authority_outage(tmp_path: Path) -> None:
    authority = _MemoryBaselineAuthorityStore()
    _seed_attested_prepare_checkpoint(tmp_path, authority)
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
    _seed_attested_prepare_checkpoint(tmp_path, authority)
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
async def test_start_task_reconstructs_task_text_from_legacy_understanding(
    tmp_path: Path,
) -> None:
    runtime = _stub_runtime(tmp_path)
    runtime.state.task_text = None
    runtime.state.task_understanding = {
        "title": "Kaggriculture farming simulation",
        "dataset": "kaggriculture environment",
        "target": "maximize income",
    }
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = SimpleNamespace(done=lambda: False)

    status = await runtime.start_task("continue")

    assert status == "RUNNING"
    assert runtime.task_text == (
        "Kaggriculture farming simulation kaggriculture environment maximize income"
    )
    assert runtime.state.task_text is None


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

    await runtime.message("/resume")
    assert started == []
