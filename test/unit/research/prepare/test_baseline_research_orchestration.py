"""Tests for the PREPARE baseline evidence gate."""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.prepare import baseline
from athena.research.prepare.baseline_research import (
    BaselineResearchError,
    BaselineVerification,
    VerifiedBaseline,
    load_baseline_artifacts,
    research_sha256,
    write_verification,
)

from test.unit.research.prepare.test_baseline_research_contract import (
    valid_payload,
    write_artifacts,
)


class FakeAgents:
    """Record the one-shot ideator cleanup requested by the gate."""

    def __init__(self) -> None:
        self.reaped: list[str] = []

    async def reap(self, agent_id: str) -> None:
        self.reaped.append(agent_id)


class FakeHandoff:
    """Write complete baseline artifacts for each scripted agent turn."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        write_artifacts(self.root)
        return "ok"


class FakeVerifier:
    """Return queued verification outcomes without network access."""

    def __init__(self, outcomes: list[BaselineVerification | Exception]) -> None:
        self.outcomes = list(outcomes)

    async def verify(self, artifacts):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome.model_copy(
            update={"research_sha256": research_sha256(artifacts.raw_research)}
        )


def _runtime() -> SimpleNamespace:
    """Build the minimal runtime used by baseline orchestration tests."""
    return SimpleNamespace(
        agents=FakeAgents(),
        registry=SimpleNamespace(contains=lambda _name: False),
        provider=object(),
        store=object(),
        execution=object(),
        publish_output=_publish_output,
    )


async def _publish_output(**_kwargs) -> None:
    """Collect no-op runtime output."""


def _verification() -> BaselineVerification:
    """Return a valid fake Git verification record."""
    return BaselineVerification(
        research_sha256="0" * 64,
        selected_candidate_id="resnet-transfer",
        route="git",
        verified_at=datetime.now(timezone.utc),
        repository_url="https://github.com/pytorch/vision.git",
        commit="a" * 40,
        attempts=[{"route": "git", "success": True, "diagnostic": "verified"}],
    )


def _workspace(root: Path) -> SimpleNamespace:
    return SimpleNamespace(path=str(root))


@pytest.mark.asyncio
async def test_valid_first_response_writes_verification_and_reaps(
    tmp_path: Path, monkeypatch
) -> None:
    """A valid first response reaches the gate output and cleans up the ideator."""
    runtime = _runtime()
    handoff = FakeHandoff(tmp_path)
    verifier = FakeVerifier([_verification()])
    monkeypatch.setattr(baseline, "_register_ideator", lambda *_args: None)

    result = await baseline.prepare_baseline_design(
        runtime, _workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert result.verification.route == "git"
    assert (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").is_file()
    assert runtime.agents.reaped == ["baseline_ideator"]
    assert len(handoff.calls) == 1


@pytest.mark.asyncio
async def test_invalid_first_response_gets_exactly_one_repair(
    tmp_path: Path, monkeypatch
) -> None:
    """A verifier rejection receives one deterministic repair request."""
    runtime = _runtime()
    handoff = FakeHandoff(tmp_path)
    verifier = FakeVerifier(
        [BaselineResearchError("bad source", ["clone failed"]), _verification()]
    )
    monkeypatch.setattr(baseline, "_register_ideator", lambda *_args: None)

    await baseline.prepare_baseline_design(
        runtime, _workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(handoff.calls) == 2
    assert "clone failed" in handoff.calls[1]["content"]
    assert "rewrite both complete artifacts" in handoff.calls[1]["content"]


@pytest.mark.asyncio
async def test_two_invalid_responses_stop_before_prepare(
    tmp_path: Path, monkeypatch
) -> None:
    """Two rejected responses produce a terminal typed gate error."""
    runtime = _runtime()
    handoff = FakeHandoff(tmp_path)
    verifier = FakeVerifier(
        [
            BaselineResearchError("bad one", ["first"]),
            BaselineResearchError("bad two", ["second"]),
        ]
    )
    monkeypatch.setattr(baseline, "_register_ideator", lambda *_args: None)

    with pytest.raises(BaselineResearchError, match="bad two"):
        await baseline.prepare_baseline_design(
            runtime, _workspace(tmp_path), "task", True, handoff, verifier=verifier
        )
    assert len(handoff.calls) == 2
    assert runtime.agents.reaped == ["baseline_ideator"]


@pytest.mark.asyncio
async def test_invalid_resumed_artifacts_receive_one_repair_turn(
    tmp_path: Path, monkeypatch
) -> None:
    """Resumed artifacts get one repair, not a fresh two-turn research loop."""
    write_artifacts(tmp_path)
    runtime = _runtime()
    handoff = FakeHandoff(tmp_path)
    verifier = FakeVerifier(
        [
            BaselineResearchError("stale source", ["source rejected"]),
            _verification(),
        ]
    )
    monkeypatch.setattr(baseline, "_register_ideator", lambda *_args: None)

    await baseline.prepare_baseline_design(
        runtime, _workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert len(handoff.calls) == 1
    assert "source rejected" in handoff.calls[0]["content"]


@pytest.mark.asyncio
async def test_eda_not_ready_is_a_hard_gate(tmp_path: Path) -> None:
    """Baseline research cannot silently fall back when EDA is unavailable."""
    with pytest.raises(BaselineResearchError, match="EDA"):
        await baseline.prepare_baseline_design(
            _runtime(),
            _workspace(tmp_path),
            "task",
            False,
            FakeHandoff(tmp_path),
            verifier=FakeVerifier([]),
        )


@pytest.mark.asyncio
async def test_matching_cache_skips_agent_and_wrong_digest_revalidates(
    tmp_path: Path, monkeypatch
) -> None:
    """A matching cache skips the ideator while a stale digest is rechecked."""
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    write_verification(
        tmp_path,
        _verification().model_copy(
            update={"research_sha256": research_sha256(artifacts.raw_research)}
        ),
    )
    runtime = _runtime()
    handoff = FakeHandoff(tmp_path)
    monkeypatch.setattr(baseline, "_register_ideator", lambda *_args: None)
    result = await baseline.prepare_baseline_design(
        runtime,
        _workspace(tmp_path),
        "task",
        True,
        handoff,
        verifier=FakeVerifier([_verification()]),
    )
    assert result.verification.route == "git"
    assert handoff.calls == []

    (tmp_path / "BASELINE_RESEARCH.json").write_text(
        json.dumps(valid_payload(), sort_keys=True), encoding="utf-8"
    )
    assert research_sha256((tmp_path / "BASELINE_RESEARCH.json").read_bytes()) != (
        result.verification.research_sha256
    )


def test_verified_task_contains_provenance() -> None:
    """Prepare receives all artifact names and source provenance."""
    artifacts = SimpleNamespace(
        selected=SimpleNamespace(candidate_id="resnet-transfer"),
        design=SimpleNamespace(training_strategy="partial_finetune"),
    )
    verified = VerifiedBaseline(artifacts, _verification())
    task = baseline.verified_baseline_task("task", verified)
    assert "BASELINE_RESEARCH.json" in task
    assert "BASELINE_RESEARCH_VERIFICATION.json" in task
    assert "BASELINE_DESIGN.md" in task
    assert "resnet-transfer" in task
    assert "partial_finetune" in task
    assert "git" in task
    assert "a" * 40 in task


@pytest.mark.asyncio
async def test_run_baseline_checks_research_before_registration(
    tmp_path: Path, monkeypatch
) -> None:
    """A changed research file cannot reach prepare-agent registration."""
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    verified = VerifiedBaseline(
        artifacts,
        _verification().model_copy(
            update={"research_sha256": research_sha256(artifacts.raw_research)}
        ),
    )
    (tmp_path / "BASELINE_RESEARCH.json").write_text("{}", encoding="utf-8")
    registered: list[str] = []
    monkeypatch.setattr(
        baseline,
        "_register_prepare_agent",
        lambda *_args: registered.append("prepare"),
    )

    with pytest.raises(BaselineResearchError, match="digest"):
        await baseline.run_baseline(
            _runtime(), _workspace(tmp_path), "eval", "task", None, verified
        )
    assert registered == []
