"""End-to-end authoritative-research gate into trusted PREPARE scoring."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.tool import ToolRegistry
from athena.research.evaluation import TrustedEvaluator
from athena.research.literature.paper_source.http import UrllibTransport
from athena.research.literature.paper_source.openalex import OpenAlexWork
from athena.research.prepare import baseline, orchestrator
from athena.research.prepare.baseline_research import (
    BaselineResearchError,
    BaselineVerification,
)
from athena.research.prepare.orchestrator import run_prepare_phase
from athena.research.prepare.source_verification import (
    BaselineSourceVerifier,
    GitCloneEvidence,
    GitCloneVerifier,
)
from athena.research.runtime.phase_runner import PhaseRunner
from test.integration.research.test_prepare_agent_contract import (
    _Harness as _PrepareHarness,
)


def _research_payload(*, repository_url: str | None) -> dict[str, Any]:
    """Return one complete research record with hand-checked expected values."""
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
            "evidence": ["eda:EDA_HANDOFF.md: 480 labels across 120 groups"],
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
                "repository_url": repository_url,
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


class _BaselineIdeatorProvider:
    """Script complete or malformed artifact pairs through the real Agent tools."""

    model_name = "baseline-ideator-integration"

    def __init__(self, *, responses: list[str], repository_url: str | None) -> None:
        self.responses = list(responses)
        self.repository_url = repository_url
        self.calls = 0
        self.turn = 0
        self.config_names: list[str] = []
        self._actions: list[tuple[str, str] | None] = []

    def _turn_actions(self, response: str) -> list[tuple[str, str] | None]:
        if response != "valid":
            return [
                ("BASELINE_RESEARCH.json", response),
                ("BASELINE_DESIGN.md", response),
                None,
            ]
        return [
            (
                "BASELINE_RESEARCH.json",
                json.dumps(_research_payload(repository_url=self.repository_url)),
            ),
            (
                "BASELINE_DESIGN.md",
                "# Baseline\n\nSelected candidate: `resnet-transfer`\n"
                "Training strategy: `partial_finetune`\n",
            ),
            None,
        ]

    async def stream(self, config, _tools, _messages, _cancel, **_kwargs):
        self.calls += 1
        self.config_names.append(config.name)
        if not self._actions:
            self.turn += 1
            response = self.responses[self.turn - 1]
            self._actions = self._turn_actions(response)
        action = self._actions.pop(0)
        if action is None:
            answer = json.dumps(
                {
                    "summary": "baseline research written",
                    "handoff_file": "BASELINE_DESIGN.md",
                }
            )
            yield StreamEvent(
                kind="text_delta", data={"delta": answer, "accumulated": answer}
            )
        else:
            path, content = action
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": f"research-write-{self.calls}",
                    "name": "write_file",
                    "arguments": {"path": path, "content": content},
                },
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


class _ProviderRouter:
    """Route two real agent registrations to independently inspectable fakes."""

    model_name = "authoritative-gate-integration"

    def __init__(self, research: _BaselineIdeatorProvider, prepare: Any) -> None:
        self.research = research
        self.prepare = prepare

    async def stream(self, config, tools, messages, cancel, **kwargs):
        provider = (
            self.research if config.name == "baseline_ideator-agent" else self.prepare
        )
        async for event in provider.stream(config, tools, messages, cancel, **kwargs):
            yield event


class _FakeGit:
    def __init__(
        self,
        *,
        commit: str | None,
        error: BaselineResearchError | None = None,
    ) -> None:
        self.commit = commit
        self.error = error
        self.urls: list[str] = []

    async def verify(self, repository_url: str) -> GitCloneEvidence:
        self.urls.append(repository_url)
        if self.error is not None:
            raise self.error
        if self.commit is None:
            raise BaselineResearchError(
                "Git source verification failed", ["fake clone unavailable"]
            )
        return GitCloneEvidence(repository_url=repository_url, commit=self.commit)


class _FakeOpenAlex:
    def __init__(
        self,
        *,
        citations: int | None,
        error: BaseException | None = None,
    ) -> None:
        self.citations = citations
        self.error = error
        self.locators: list[str] = []

    async def fetch_work(self, locator: str) -> OpenAlexWork | None:
        self.locators.append(locator)
        if self.error is not None:
            raise self.error
        if self.citations is None:
            return None
        return OpenAlexWork(
            openalex_id="W123",
            title="Deep Residual Learning for Image Recognition",
            publication_year=2016,
            cited_by_count=self.citations,
        )


class _RecordingRegistry(AgentTypeRegistry):
    """Record whether platform verification existed at PREPARE registration."""

    def __init__(self, workspace: Path) -> None:
        super().__init__()
        self.workspace = workspace
        self.prepare_registration_verified: list[bool] = []

    def register(self, agent_type, factory) -> None:
        if agent_type == "prepare":
            self.prepare_registration_verified.append(
                (self.workspace / "BASELINE_RESEARCH_VERIFICATION.json").is_file()
            )
        super().register(agent_type, factory)


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, dict | None]] = []

    async def project_agent_event(
        self, agent_id: str, kind: str, ref: str, data: dict | None
    ) -> None:
        self.items.append((agent_id, kind, ref, data))


class _GateHarness(_PrepareHarness):
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(tmp_path)
        self.monkeypatch = monkeypatch
        self.external_network_calls: list[str] = []
        self.handoff_calls: list[dict[str, Any]] = []

    async def start(self) -> None:
        await super().start()
        root = Path(self.branch.path)
        (root / "EDA_HANDOFF.md").write_text(
            "# EDA Handoff\n\n480 labels across 120 independent groups.\n",
            encoding="utf-8",
        )

        def forbid_http(_transport, url: str, _headers: dict[str, str]):
            self.external_network_calls.append(url)
            raise AssertionError(
                f"external HTTP is forbidden in integration tests: {url}"
            )

        async def forbid_git(_verifier, repository_url: str):
            self.external_network_calls.append(repository_url)
            raise AssertionError(
                f"external Git is forbidden in integration tests: {repository_url}"
            )

        self.monkeypatch.setattr(UrllibTransport, "get_sync", forbid_http)
        self.monkeypatch.setattr(GitCloneVerifier, "verify", forbid_git)

    async def run(
        self,
        *,
        git_commit: str | None = None,
        git_failure: bool = False,
        citations: int | None = None,
        openalex_error: BaseException | None = None,
        repository_url: str | None = "https://github.com/pytorch/vision.git",
        research_responses: list[str] | None = None,
    ):
        evaluator_ref = await self.freeze_evaluator()
        root = Path(self.branch.path)
        self.research_provider = _BaselineIdeatorProvider(
            responses=research_responses or ["valid"],
            repository_url=repository_url,
        )
        provider = _ProviderRouter(self.research_provider, self.prepare_provider)
        git_error = (
            BaselineResearchError("Git source verification failed", ["clone failed"])
            if git_failure
            else None
        )
        self.fake_git = _FakeGit(commit=git_commit, error=git_error)
        self.fake_openalex = _FakeOpenAlex(citations=citations, error=openalex_error)
        verifier = BaselineSourceVerifier(
            git=self.fake_git,
            openalex=self.fake_openalex,
            now=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        self.registry = _RecordingRegistry(root)
        self.agents = self._agent_runtime(self.registry)
        self.events = _Events()
        outputs: list[dict[str, Any]] = []

        async def publish_output(**kwargs: Any) -> None:
            outputs.append(kwargs)

        runtime = SimpleNamespace(
            prepare_phase=None,
            provider=provider,
            task_text="inspect data and build a trusted baseline",
            registry=self.registry,
            agents=self.agents,
            store=self.store,
            execution=self.execution,
            git=self.git,
            evaluator=TrustedEvaluator(self.scripts),
            tree=SimpleNamespace(to_dict=lambda: {"experiments": []}),
            events=self.events,
            task_confirmation_gate=False,
            baseline_ideator_tools=lambda: ToolRegistry(),
            kaggle_tools=lambda _kind: None,
            publish_output=publish_output,
        )
        self.runtime_facade = runtime
        self.outputs = outputs
        self.agents.start()
        handoff = PhaseRunner(runtime)._run_handoff_agent

        async def recording_handoff(**kwargs: Any) -> str:
            self.handoff_calls.append(dict(kwargs))
            return await handoff(**kwargs)

        async def prepared_workspace(_runtime: Any):
            return self.branch

        async def no_data_contract(_runtime: Any):
            return None

        async def frozen_evaluators(_runtime: Any, _task: str):
            return SimpleNamespace(search_ref=evaluator_ref, final_ref=evaluator_ref)

        async def seeded_eda(
            _runtime: Any, workspace: Any, _handoff: Any, _task: str
        ) -> bool:
            assert (Path(workspace.path) / "EDA_HANDOFF.md").is_file()
            return True

        self.monkeypatch.setattr(
            baseline, "build_default_source_verifier", lambda: verifier
        )
        self.monkeypatch.setattr(orchestrator, "prepare_workspace", prepared_workspace)
        self.monkeypatch.setattr(
            orchestrator, "prepare_platform_split", no_data_contract
        )
        self.monkeypatch.setattr(orchestrator, "prepare_evaluators", frozen_evaluators)
        self.monkeypatch.setattr(orchestrator, "prepare_eda", seeded_eda)
        return await run_prepare_phase(runtime, recording_handoff)


@pytest_asyncio.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gate = _GateHarness(tmp_path, monkeypatch)
    await gate.start()
    try:
        yield gate
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_git_qualified_research_reaches_trusted_baseline(harness) -> None:
    result = await harness.run(git_commit="c" * 40)
    root = Path(harness.branch.path)
    verification = BaselineVerification.model_validate_json(
        (root / "BASELINE_RESEARCH_VERIFICATION.json").read_text(encoding="utf-8")
    )
    assert verification.route == "git"
    assert verification.commit == "c" * 40
    assert result.metric == 1.0
    assert "resnet-transfer" in (root / "RESEARCH_HANDOFF.md").read_text(
        encoding="utf-8"
    )
    assert harness.registry.prepare_registration_verified == [True]
    assert harness.external_network_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("citations", "passes"), [(100, True), (99, False)])
async def test_repository_free_openalex_threshold_is_inclusive(
    harness, citations: int, passes: bool
) -> None:
    if passes:
        result = await harness.run(repository_url=None, citations=citations)
        assert result.metric == 1.0
        verification = BaselineVerification.model_validate_json(
            (
                Path(harness.branch.path) / "BASELINE_RESEARCH_VERIFICATION.json"
            ).read_text(encoding="utf-8")
        )
        assert verification.route == "openalex"
        assert verification.cited_by_count == 100
        assert harness.registry.prepare_registration_verified == [True]
    else:
        with pytest.raises(BaselineResearchError, match="no qualifying source"):
            await harness.run(
                repository_url=None,
                citations=citations,
                research_responses=["valid", "valid"],
            )
        assert harness.prepare_provider.calls == 0
        assert harness.registry.prepare_registration_verified == []
    assert harness.fake_git.urls == []
    assert harness.external_network_calls == []


@pytest.mark.asyncio
async def test_git_failure_falls_back_to_openalex(harness) -> None:
    result = await harness.run(git_failure=True, citations=137)
    verification = BaselineVerification.model_validate_json(
        (Path(harness.branch.path) / "BASELINE_RESEARCH_VERIFICATION.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.metric == 1.0
    assert verification.route == "openalex"
    assert verification.openalex_id == "W123"
    assert [(attempt.route, attempt.success) for attempt in verification.attempts] == [
        ("git", False),
        ("openalex", True),
    ]
    assert harness.external_network_calls == []


@pytest.mark.asyncio
async def test_git_failure_then_openalex_exception_never_starts_prepare(
    harness,
) -> None:
    with pytest.raises(BaselineResearchError, match="no qualifying source"):
        await harness.run(
            git_failure=True,
            openalex_error=RuntimeError("offline authority unavailable"),
            research_responses=["valid", "valid"],
        )
    assert harness.prepare_provider.calls == 0
    assert harness.registry.prepare_registration_verified == []
    assert harness.external_network_calls == []


@pytest.mark.asyncio
async def test_invalid_first_response_repairs_same_baseline_ideator(harness) -> None:
    result = await harness.run(
        git_commit="d" * 40, research_responses=["invalid", "valid"]
    )
    assert result.metric == 1.0
    assert harness.research_provider.turn == 2
    research_calls = [
        call
        for call in harness.handoff_calls
        if call["agent_type"] == "baseline_ideator"
    ]
    assert {call["agent_id"] for call in research_calls} == {"baseline_ideator"}
    assert len(research_calls) == 2


@pytest.mark.asyncio
async def test_two_research_failures_never_start_prepare(harness) -> None:
    with pytest.raises(BaselineResearchError):
        await harness.run(research_responses=["invalid", "still invalid"])
    assert harness.prepare_provider.calls == 0
    assert harness.registry.prepare_registration_verified == []
    assert harness.external_network_calls == []


@pytest.mark.asyncio
async def test_prepare_receives_and_writes_verified_provenance(harness) -> None:
    await harness.run(git_commit="e" * 40)
    root = Path(harness.branch.path)
    task = harness.prepare_provider.task_seen
    assert "Selected candidate: resnet-transfer" in task
    assert "Verification route: git" in task
    assert f"Verified revision: {'e' * 40}" in task
    assert "Training strategy: partial_finetune" in task
    report = (root / "report.md").read_text(encoding="utf-8")
    handoff = (root / "RESEARCH_HANDOFF.md").read_text(encoding="utf-8")
    for evidence in ("resnet-transfer", "git", "partial_finetune", "e" * 40):
        assert evidence in report
        assert evidence in handoff
    assert harness.external_network_calls == []
