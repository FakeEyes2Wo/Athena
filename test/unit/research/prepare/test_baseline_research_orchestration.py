"""Two-turn baseline research and verified PREPARE orchestration contracts."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from athena.research.prepare import baseline
from athena.research.prepare.baseline import prepare_baseline_design, run_baseline
from athena.research.prepare.baseline_research import (
    BaselineArtifacts,
    BaselineResearchError,
    BaselineVerification,
    VerifiedBaseline,
    load_baseline_artifacts,
    research_sha256,
    write_verification,
)


def valid_payload() -> dict[str, Any]:
    """Return one complete, hand-checked research artifact."""
    return {
        "schema_version": 1,
        "dataset": {
            "modality": "image",
            "task_type": "classification",
            "labeled_samples": 480,
            "effective_training_units": 120,
            "group_count": 120,
            "class_count": 5,
            "minority_class_samples": 32,
            "input_scale": "paired 224x224 images",
            "regime": "small",
            "recommended_strategy": "partial_finetune",
            "evidence": ["eda:EDA_REPORT_LABELS.md: 480 labels across 120 groups"],
            "rationale": "Grouped labels are limited relative to pretrained capacity.",
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
        "search_queries": ["small image classification transfer baseline GitHub"],
        "limitations": [],
    }


def write_artifacts(root: Path, *, changed: bool = False) -> None:
    """Write a matching artifact pair."""
    payload = valid_payload()
    if changed:
        payload["limitations"] = ["research changed"]
    (root / "BASELINE_RESEARCH.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "BASELINE_DESIGN.md").write_text(
        "# Baseline\n\nSelected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n",
        encoding="utf-8",
    )


def valid_verification(artifacts: BaselineArtifacts) -> BaselineVerification:
    """Build deterministic valid Git evidence for the supplied bytes."""
    return BaselineVerification(
        research_sha256=research_sha256(artifacts.raw_research),
        selected_candidate_id=artifacts.selected.candidate_id,
        route="git",
        verified_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        repository_url=str(artifacts.selected.repository_url),
        commit="a" * 40,
        attempts=[{"route": "git", "success": True, "diagnostic": "verified"}],
    )


def write_forged_verification(root: Path) -> None:
    """Simulate an agent forging a complete, digest-matching platform record."""
    write_verification(root, valid_verification(load_baseline_artifacts(root)))


class FakeAgents:
    def __init__(self) -> None:
        self.reaped: list[str] = []

    async def reap(self, agent_id: str) -> None:
        self.reaped.append(agent_id)


class FakeRuntime:
    def __init__(self) -> None:
        self.agents = FakeAgents()
        self.outputs: list[dict[str, Any]] = []
        self.registry = SimpleNamespace(contains=lambda _agent_type: True)
        self.provider = object()
        self.store = object()
        self.execution = object()

    def baseline_ideator_tools(self) -> object:
        return object()

    async def publish_output(self, **kwargs: Any) -> None:
        self.outputs.append(kwargs)


Writer = Callable[[Path], None]


class ScriptedHandoff:
    def __init__(self, root: Path, responses: list[Writer | BaseException]) -> None:
        self.root = root
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        response(self.root)
        design = self.root / "BASELINE_DESIGN.md"
        return design.read_text(encoding="utf-8") if design.is_file() else ""


class QueuedVerifier:
    def __init__(self, root: Path, outcomes: list[str | BaseException]) -> None:
        self.root = root
        self.outcomes = list(outcomes)
        self.calls: list[BaselineArtifacts] = []
        self.verification_file_seen: list[bool] = []

    async def verify(self, artifacts: BaselineArtifacts) -> BaselineVerification:
        self.calls.append(artifacts)
        self.verification_file_seen.append(
            (self.root / "BASELINE_RESEARCH_VERIFICATION.json").exists()
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome == "valid"
        return valid_verification(artifacts)


def workspace(root: Path) -> SimpleNamespace:
    return SimpleNamespace(path=str(root), base_commit="base-commit")


def writes_valid(root: Path) -> None:
    write_artifacts(root)


def writes_missing_research(root: Path) -> None:
    (root / "BASELINE_DESIGN.md").write_text("incomplete", encoding="utf-8")


def writes_invalid_json(root: Path) -> None:
    (root / "BASELINE_RESEARCH.json").write_text("{}", encoding="utf-8")
    (root / "BASELINE_DESIGN.md").write_text("incomplete", encoding="utf-8")


def writes_valid_and_forged_verification(root: Path) -> None:
    write_artifacts(root)
    write_forged_verification(root)


@pytest.mark.asyncio
async def test_valid_first_response_writes_verification_and_reaps(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [writes_valid])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    result = await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert result.verification.route == "git"
    assert (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").is_file()
    assert runtime.agents.reaped == ["baseline_ideator"]
    assert len(handoff.calls) == 1
    assert handoff.calls[0]["reap_after"] is False
    assert verifier.verification_file_seen == [False]


@pytest.mark.asyncio
async def test_invalid_first_response_gets_exactly_one_repair(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [writes_valid, writes_valid])
    verifier = QueuedVerifier(
        tmp_path,
        [BaselineResearchError("bad source", ["clone failed"]), "valid"],
    )

    await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(handoff.calls) == 2
    assert "clone failed" in handoff.calls[1]["content"]
    assert "rewrite both complete artifacts" in handoff.calls[1]["content"]
    assert {call["agent_id"] for call in handoff.calls} == {"baseline_ideator"}
    assert all(call["reap_after"] is False for call in handoff.calls)


@pytest.mark.asyncio
async def test_two_invalid_responses_publish_terminal_error_and_leave_no_verification(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [writes_valid, writes_valid])
    verifier = QueuedVerifier(
        tmp_path,
        [
            BaselineResearchError("bad source", ["clone failed"]),
            BaselineResearchError("still bad", ["OpenAlex unresolved"]),
        ],
    )

    with pytest.raises(BaselineResearchError, match="still bad"):
        await prepare_baseline_design(
            runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
        )

    assert len(handoff.calls) == 2
    assert runtime.agents.reaped == ["baseline_ideator"]
    assert runtime.outputs[-1]["channel"] == "error"
    assert "PREPARE" in runtime.outputs[-1]["text"]
    assert not (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").exists()


@pytest.mark.asyncio
async def test_eda_unavailable_is_a_hard_failure(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [])
    verifier = QueuedVerifier(tmp_path, [])

    with pytest.raises(BaselineResearchError, match="EDA"):
        await prepare_baseline_design(
            runtime, workspace(tmp_path), "task", False, handoff, verifier=verifier
        )

    assert handoff.calls == []
    assert runtime.agents.reaped == []
    assert runtime.outputs[-1]["channel"] == "error"


@pytest.mark.asyncio
async def test_missing_research_file_receives_repair_instead_of_fallback(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [writes_missing_research, writes_valid])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(handoff.calls) == 2
    assert "BASELINE_RESEARCH.json" in handoff.calls[1]["content"]
    assert "using the task only" not in " ".join(
        str(output["text"]) for output in runtime.outputs
    )


@pytest.mark.asyncio
async def test_matching_cache_returns_without_verifier_or_agent(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    write_forged_verification(tmp_path)
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [])
    verifier = QueuedVerifier(tmp_path, [])

    result = await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert result.verification.commit == "a" * 40
    assert verifier.calls == []
    assert handoff.calls == []
    assert runtime.agents.reaped == []


@pytest.mark.asyncio
async def test_matching_cache_returns_before_default_verifier_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_artifacts(tmp_path)
    write_forged_verification(tmp_path)

    class ForbiddenAgents(FakeAgents):
        async def reap(self, agent_id: str) -> None:
            raise AssertionError(f"cache hit must not reap {agent_id}")

    runtime = FakeRuntime()
    runtime.agents = ForbiddenAgents()

    async def forbidden_handoff(**_kwargs: Any) -> str:
        raise AssertionError("cache hit must not call the ideator")

    monkeypatch.setattr(
        baseline,
        "build_default_source_verifier",
        lambda: (_ for _ in ()).throw(
            AssertionError("cache hit must not construct a verifier")
        ),
    )
    monkeypatch.setattr(
        baseline,
        "_register_ideator",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("cache hit must not register the ideator")
        ),
    )

    result = await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, forbidden_handoff
    )

    assert result.verification.commit == "a" * 40


@pytest.mark.asyncio
async def test_wrong_digest_cache_is_removed_and_revalidated(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    stale = valid_verification(load_baseline_artifacts(tmp_path)).model_copy(
        update={"research_sha256": "0" * 64}
    )
    write_verification(tmp_path, stale)
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    result = await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(verifier.calls) == 1
    assert verifier.verification_file_seen == [False]
    assert result.verification.research_sha256 != "0" * 64
    assert handoff.calls == []


@pytest.mark.asyncio
async def test_preexisting_unverified_artifacts_verify_before_agent_call(
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(verifier.calls) == 1
    assert handoff.calls == []
    assert runtime.agents.reaped == []


@pytest.mark.asyncio
async def test_invalid_resumed_artifacts_receive_only_one_repair_turn(
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [writes_valid])
    verifier = QueuedVerifier(
        tmp_path,
        [BaselineResearchError("bad resumed source", ["timeout"]), "valid"],
    )

    await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(handoff.calls) == 1
    assert "timeout" in handoff.calls[0]["content"]
    assert "Use web and paper tools" not in handoff.calls[0]["content"]


@pytest.mark.asyncio
async def test_no_artifacts_get_one_initial_turn_and_one_repair(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [writes_invalid_json, writes_valid])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(handoff.calls) == 2
    assert "Use web and paper tools" in handoff.calls[0]["content"]
    assert "invalid BASELINE_RESEARCH.json" in handoff.calls[1]["content"]


@pytest.mark.asyncio
async def test_handoff_failure_becomes_bounded_repair_diagnostic(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(
        tmp_path, [RuntimeError("provider unavailable"), writes_valid]
    )
    verifier = QueuedVerifier(tmp_path, ["valid"])

    await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(handoff.calls) == 2
    assert "provider unavailable" in handoff.calls[1]["content"]


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_or_swallowed(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(tmp_path, [asyncio.CancelledError()])
    verifier = QueuedVerifier(tmp_path, [])

    with pytest.raises(asyncio.CancelledError):
        await prepare_baseline_design(
            runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
        )

    assert runtime.agents.reaped == ["baseline_ideator"]
    assert runtime.outputs == []


@pytest.mark.asyncio
async def test_reap_timeout_preserves_success_and_eventually_forgets_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = asyncio.Event()
    removed = asyncio.Event()

    class StatefulAgents(FakeAgents):
        def __init__(self) -> None:
            super().__init__()
            self.live = {"baseline_ideator"}
            self.cancelled = False

        async def reap(self, agent_id: str) -> None:
            self.reaped.append(agent_id)
            try:
                await release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            self.live.remove(agent_id)
            removed.set()

    runtime = FakeRuntime()
    runtime.agents = StatefulAgents()
    handoff = ScriptedHandoff(tmp_path, [writes_valid])
    verifier = QueuedVerifier(tmp_path, ["valid"])
    monkeypatch.setattr(baseline, "_IDEATOR_REAP_TIMEOUT_SECONDS", 0.01)

    result = await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert result.verification.route == "git"
    assert runtime.agents.reaped == ["baseline_ideator"]
    assert runtime.agents.live == {"baseline_ideator"}
    assert runtime.agents.cancelled is False

    release.set()
    await asyncio.wait_for(removed.wait(), timeout=1.0)
    assert runtime.agents.live == set()


@pytest.mark.asyncio
async def test_reap_timeout_preserves_terminal_error_and_eventually_forgets_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = asyncio.Event()
    removed = asyncio.Event()

    class StatefulAgents(FakeAgents):
        def __init__(self) -> None:
            super().__init__()
            self.live = {"baseline_ideator"}

        async def reap(self, agent_id: str) -> None:
            self.reaped.append(agent_id)
            await release.wait()
            self.live.remove(agent_id)
            removed.set()

    runtime = FakeRuntime()
    runtime.agents = StatefulAgents()
    handoff = ScriptedHandoff(tmp_path, [writes_valid, writes_valid])
    verifier = QueuedVerifier(
        tmp_path,
        [
            BaselineResearchError("bad source", ["clone failed"]),
            BaselineResearchError("still bad", ["OpenAlex unresolved"]),
        ],
    )
    monkeypatch.setattr(baseline, "_IDEATOR_REAP_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(BaselineResearchError, match="still bad"):
        await prepare_baseline_design(
            runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
        )

    assert runtime.agents.live == {"baseline_ideator"}
    release.set()
    await asyncio.wait_for(removed.wait(), timeout=1.0)
    assert runtime.agents.live == set()


@pytest.mark.asyncio
async def test_reap_error_is_observed_without_replacing_success(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    class RecoverableErrorAgents(FakeAgents):
        def __init__(self) -> None:
            super().__init__()
            self.live = {"baseline_ideator"}

        async def reap(self, agent_id: str) -> None:
            self.reaped.append(agent_id)
            self.live.remove(agent_id)
            raise RuntimeError("facade close bookkeeping warning")

    runtime = FakeRuntime()
    runtime.agents = RecoverableErrorAgents()
    handoff = ScriptedHandoff(tmp_path, [writes_valid])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    with caplog.at_level("WARNING", logger=baseline.__name__):
        result = await prepare_baseline_design(
            runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
        )
        await asyncio.sleep(0)

    assert result.verification.route == "git"
    assert runtime.agents.live == set()
    assert "facade close bookkeeping warning" in caplog.text


@pytest.mark.asyncio
async def test_terminal_agent_forgery_is_removed_and_restart_revalidates(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    handoff = ScriptedHandoff(
        tmp_path,
        [writes_valid_and_forged_verification, writes_valid_and_forged_verification],
    )
    verifier = QueuedVerifier(
        tmp_path,
        [
            BaselineResearchError("bad source", ["clone failed"]),
            BaselineResearchError("still bad", ["OpenAlex unresolved"]),
        ],
    )

    with pytest.raises(BaselineResearchError, match="still bad"):
        await prepare_baseline_design(
            runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
        )

    verification_path = tmp_path / "BASELINE_RESEARCH_VERIFICATION.json"
    assert verifier.verification_file_seen == [False, False]
    assert not verification_path.exists()

    restarted_runtime = FakeRuntime()
    restarted_handoff = ScriptedHandoff(tmp_path, [])
    restarted_verifier = QueuedVerifier(tmp_path, ["valid"])
    result = await prepare_baseline_design(
        restarted_runtime,
        workspace(tmp_path),
        "task",
        True,
        restarted_handoff,
        verifier=restarted_verifier,
    )

    assert result.verification.route == "git"
    assert len(restarted_verifier.calls) == 1
    assert restarted_handoff.calls == []


def test_register_ideator_uses_baseline_only_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}
    tools = object()
    runtime = SimpleNamespace(
        registry=SimpleNamespace(contains=lambda _agent_type: False),
        provider=object(),
        store=object(),
        execution=object(),
        baseline_ideator_tools=lambda: tools,
        ideator_tools=lambda: (_ for _ in ()).throw(
            AssertionError("ordinary ideator tools must not be used")
        ),
    )
    monkeypatch.setattr(
        baseline,
        "register_ideator_agent",
        lambda *args, **kwargs: captured.update(kwargs),
    )

    baseline._register_ideator(runtime, tmp_path)

    assert captured["extra_tools"] is tools


def verified_fixture(root: Path, route: str = "git") -> VerifiedBaseline:
    write_artifacts(root)
    artifacts = load_baseline_artifacts(root)
    if route == "git":
        verification = valid_verification(artifacts)
    else:
        assert route == "openalex"
        verification = BaselineVerification(
            research_sha256=research_sha256(artifacts.raw_research),
            selected_candidate_id=artifacts.selected.candidate_id,
            route="openalex",
            verified_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
            openalex_id="https://openalex.org/W2741809807",
            title="Deep Residual Learning for Image Recognition",
            publication_year=2016,
            cited_by_count=100000,
            attempts=[
                {
                    "route": "openalex",
                    "success": True,
                    "diagnostic": "authority verified",
                }
            ],
        )
    write_verification(root, verification)
    return VerifiedBaseline(artifacts=artifacts, verification=verification)


class PrepareRuntime:
    def __init__(self) -> None:
        self.registry = SimpleNamespace(contains=lambda _agent_type: True)
        self.provider = object()
        self.store = SimpleNamespace(put_text=self._put_text)
        self.execution = object()
        self.tree = SimpleNamespace(to_dict=lambda: {"tree": "frozen"})
        self.agents = object()
        self.evaluator = object()
        self.git = object()
        self.events = SimpleNamespace(project_agent_event=lambda *args: None)
        self.outputs: list[dict[str, Any]] = []
        self.snapshots: list[str] = []

    async def _put_text(self, text: str) -> str:
        self.snapshots.append(text)
        return "tree-ref"

    async def publish_output(self, **kwargs: Any) -> None:
        self.outputs.append(kwargs)


async def assert_rejected_without_prepare_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    verified: VerifiedBaseline,
    *,
    match: str,
) -> None:
    runtime = PrepareRuntime()
    registered: list[str] = []
    plan_calls: list[dict[str, Any]] = []

    async def fake_run_prepare_plan(**kwargs: Any) -> str:
        plan_calls.append(kwargs)
        return "forbidden"

    monkeypatch.setattr(
        baseline,
        "_register_prepare_agent",
        lambda *_args: registered.append("prepare"),
    )
    monkeypatch.setattr(baseline, "run_prepare_plan", fake_run_prepare_plan)

    with pytest.raises(BaselineResearchError, match=match):
        await run_baseline(runtime, workspace(root), "eval", "task", None, verified)

    assert runtime.outputs == []
    assert runtime.snapshots == []
    assert registered == []
    assert plan_calls == []


def write_different_verification(
    root: Path, verified: VerifiedBaseline, difference: str
) -> None:
    values = verified.verification.model_dump(mode="json")
    if difference == "route":
        values.update(
            route="openalex",
            repository_url=None,
            commit=None,
            openalex_id="https://openalex.org/W2741809807",
            title="Deep Residual Learning for Image Recognition",
            publication_year=2016,
            cited_by_count=100000,
            attempts=[
                {
                    "route": "openalex",
                    "success": True,
                    "diagnostic": "authority verified",
                }
            ],
        )
    elif difference == "revision":
        values["commit"] = "b" * 40
    else:
        assert difference == "attempt"
        values["attempts"] = [
            {"route": "git", "success": True, "diagnostic": "different evidence"}
        ]
    write_verification(root, BaselineVerification.model_validate(values))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "replacement", "message"),
    [
        ("BASELINE_RESEARCH.json", "{}", "digest"),
        (
            "BASELINE_DESIGN.md",
            "Selected candidate: `resnet-transfer`\nTraining strategy: `classical`\n",
            "strategy",
        ),
    ],
)
async def test_run_baseline_rejects_changed_artifacts_without_prepare_side_effects(
    filename: str,
    replacement: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified = verified_fixture(tmp_path)
    (tmp_path / filename).write_text(replacement, encoding="utf-8")

    await assert_rejected_without_prepare_side_effects(
        monkeypatch, tmp_path, verified, match=message
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "difference", ["missing", "malformed", "route", "revision", "attempt"]
)
async def test_run_baseline_rejects_changed_verification_without_prepare_side_effects(
    difference: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = verified_fixture(tmp_path)
    verification_path = tmp_path / "BASELINE_RESEARCH_VERIFICATION.json"
    if difference == "missing":
        verification_path.unlink()
    elif difference == "malformed":
        verification_path.write_text("{}", encoding="utf-8")
    else:
        write_different_verification(tmp_path, verified, difference)

    await assert_rejected_without_prepare_side_effects(
        monkeypatch, tmp_path, verified, match="verification"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "revision"),
    [
        ("git", "a" * 40),
        ("openalex", "https://openalex.org/W2741809807"),
    ],
)
async def test_run_baseline_registers_only_after_check_and_enriches_task(
    route: str,
    revision: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified = verified_fixture(tmp_path, route)
    order: list[str] = []
    captured: dict[str, Any] = {}
    original_assert = baseline.assert_verified_files

    def checked(root: Path, value: VerifiedBaseline) -> None:
        original_assert(root, value)
        order.append("checked")

    def registered(*_args: Any) -> None:
        order.append("registered")

    async def fake_run_prepare_plan(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "prepared"

    monkeypatch.setattr(baseline, "assert_verified_files", checked)
    monkeypatch.setattr(baseline, "_register_prepare_agent", registered)
    monkeypatch.setattr(baseline, "run_prepare_plan", fake_run_prepare_plan)

    result = await run_baseline(
        PrepareRuntime(), workspace(tmp_path), "eval", "task", None, verified
    )

    assert result == "prepared"
    assert order == ["checked", "registered"]
    assert captured["task"].startswith("task\n\nPlatform-verified baseline artifacts:")
    for filename in (
        "BASELINE_RESEARCH.json",
        "BASELINE_RESEARCH_VERIFICATION.json",
        "BASELINE_DESIGN.md",
    ):
        assert filename in captured["task"]
    assert "Selected candidate: resnet-transfer" in captured["task"]
    assert "Training strategy: partial_finetune" in captured["task"]
    assert f"Verification route: {route}" in captured["task"]
    assert f"Verified revision: {revision}" in captured["task"]
