"""Offline integration coverage for the authoritative baseline evidence gate."""

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.prepare import baseline
from athena.research.prepare.baseline_research import (
    BaselineResearchError,
    BaselineVerification,
    VerificationAttempt,
    research_sha256,
)


def _research_payload() -> dict:
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
            "evidence": ["eda:EDA_HANDOFF.md: grouped image labels"],
            "rationale": "The grouped sample count is small for vision training.",
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
                "relevance": "A standard transfer baseline for small image data.",
            },
            {
                "candidate_id": "linear-probe",
                "title": "Transfer learning tutorial",
                "method": "frozen features with a linear head",
                "source_url": "https://docs.pytorch.org/tutorials/beginner/transfer_learning_tutorial.html",
                "source_kind": "technical_reference",
                "paper_locator": None,
                "repository_url": None,
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
        "search_queries": [
            "small image transfer baseline",
            "paired image classification paper",
        ],
        "limitations": [],
    }


def _write_artifacts(root: Path) -> None:
    (root / "BASELINE_RESEARCH.json").write_text(
        json.dumps(_research_payload()), encoding="utf-8"
    )
    (root / "BASELINE_DESIGN.md").write_text(
        "# Baseline\n\n"
        "Selected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n",
        encoding="utf-8",
    )


def _verification(root: Path, *, route: str = "git") -> BaselineVerification:
    commit = "c" * 40 if route == "git" else None
    return BaselineVerification(
        research_sha256=research_sha256((root / "BASELINE_RESEARCH.json").read_bytes()),
        selected_candidate_id="resnet-transfer",
        route=route,
        verified_at=datetime.now(timezone.utc),
        repository_url=("https://github.com/pytorch/vision.git" if commit else None),
        commit=commit,
        openalex_id=("W123" if route == "openalex" else None),
        title=(
            "Deep Residual Learning for Image Recognition"
            if route == "openalex"
            else None
        ),
        cited_by_count=(100 if route == "openalex" else None),
        attempts=[
            VerificationAttempt(route=route, success=True, diagnostic="verified")
        ],
    )


class _Agents:
    def __init__(self) -> None:
        self.reaped: list[str] = []

    async def reap(self, agent_id: str) -> None:
        self.reaped.append(agent_id)


class _Runtime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.agents = _Agents()
        self.published: list[str] = []

    async def publish_output(self, *, text: str, **_kwargs) -> None:
        self.published.append(text)


class _Handoff:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.calls: list[dict] = []
        self.responses = deque(responses or ["valid"])

    async def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        root = Path(kwargs["workspace"])
        response = self.responses.popleft()
        if response == "valid":
            _write_artifacts(root)
        else:
            (root / "BASELINE_RESEARCH.json").write_text(response, encoding="utf-8")
            (root / "BASELINE_DESIGN.md").write_text("broken", encoding="utf-8")
        return response


class _Verifier:
    def __init__(self, outcomes: list[BaselineVerification | Exception | str]) -> None:
        self.outcomes = deque(outcomes)
        self.calls = 0

    async def verify(self, artifacts):
        self.calls += 1
        outcome = self.outcomes.popleft()
        if isinstance(outcome, str):
            return _verification(artifacts.root, route=outcome)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def gate_runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(baseline, "_register_ideator", lambda *_args: None)
    return _Runtime(tmp_path), SimpleNamespace(path=tmp_path)


@pytest.mark.asyncio
async def test_git_qualified_research_writes_verification_and_reaps(
    gate_runtime,
) -> None:
    runtime, workspace = gate_runtime
    handoff = _Handoff()
    verifier = _Verifier(["git"])

    result = await baseline.prepare_baseline_design(
        runtime, workspace, "task", True, handoff, verifier=verifier
    )

    assert result.verification.route == "git"
    assert result.verification.commit == "c" * 40
    assert (workspace.path / "BASELINE_RESEARCH_VERIFICATION.json").is_file()
    assert runtime.agents.reaped == ["baseline_ideator"]
    assert len(handoff.calls) == 1


@pytest.mark.asyncio
async def test_invalid_first_response_gets_one_repair(gate_runtime) -> None:
    runtime, workspace = gate_runtime
    handoff = _Handoff(["{}", "valid"])
    verifier = _Verifier(["git"])

    await baseline.prepare_baseline_design(
        runtime, workspace, "task", True, handoff, verifier=verifier
    )

    assert len(handoff.calls) == 2
    assert "rewrite both complete artifacts" in handoff.calls[1]["content"]


@pytest.mark.asyncio
async def test_two_research_failures_never_continue_to_prepare(gate_runtime) -> None:
    runtime, workspace = gate_runtime
    handoff = _Handoff(["invalid", "still invalid"])
    verifier = _Verifier([])

    with pytest.raises(BaselineResearchError):
        await baseline.prepare_baseline_design(
            runtime, workspace, "task", True, handoff, verifier=verifier
        )

    assert len(handoff.calls) == 2
    assert verifier.calls == 0


@pytest.mark.asyncio
async def test_matching_cache_skips_baseline_agent(gate_runtime) -> None:
    runtime, workspace = gate_runtime
    _write_artifacts(workspace.path)
    verification = _verification(workspace.path)
    (workspace.path / "BASELINE_RESEARCH_VERIFICATION.json").write_text(
        verification.model_dump_json(), encoding="utf-8"
    )
    handoff = _Handoff()
    verifier = _Verifier([])

    result = await baseline.prepare_baseline_design(
        runtime, workspace, "task", True, handoff, verifier=verifier
    )

    assert result.verification.route == "git"
    assert handoff.calls == []
    assert verifier.calls == 0


@pytest.mark.asyncio
async def test_repository_free_high_citation_route_is_accepted(gate_runtime) -> None:
    runtime, workspace = gate_runtime
    _write_artifacts(workspace.path)
    verification = _verification(workspace.path, route="openalex")
    handoff = _Handoff()
    verifier = _Verifier([verification])

    result = await baseline.prepare_baseline_design(
        runtime, workspace, "task", True, handoff, verifier=verifier
    )

    assert result.verification.route == "openalex"
    assert result.verification.cited_by_count == 100
