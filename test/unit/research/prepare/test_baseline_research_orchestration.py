"""Two-turn baseline research and verified PREPARE orchestration contracts."""

import asyncio
import json
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from athena.research.prepare import baseline
from athena.research.prepare.authority import (
    BaselineAuthorityConflict,
    BaselineAuthorityError,
    PrepareAttestation,
    SealedBaseline,
    VerifiedBaselineBundle,
)
from athena.research.prepare.baseline import (
    BaselineDesignRequest,
    BaselineRunRequest,
)
from athena.research.prepare.baseline import (
    prepare_baseline_design as _prepare_baseline_design,
)
from athena.research.prepare.baseline import (
    run_baseline as _run_baseline,
)
from athena.research.prepare.baseline_research import (
    BaselineArtifacts,
    BaselineResearchError,
    BaselineVerification,
    VerificationAttempt,
    VerifiedBaseline,
    design_sha256,
    load_baseline_artifacts,
    research_sha256,
    verification_bytes,
    write_verification,
)
from athena.research.supervisor.prepare import PrepareResult


async def prepare_baseline_design(
    runtime,
    workspace,
    task,
    eda_ready,
    handoff_agent,
    *,
    verifier=None,
):
    """Keep scenario setup concise while exercising the request-based API."""
    return await _prepare_baseline_design(
        runtime,
        workspace,
        BaselineDesignRequest(task, eda_ready, handoff_agent, verifier),
    )


async def run_baseline(
    runtime, workspace, evaluator_ref, task, predict_features, verified
):
    """Keep scenario setup concise while exercising the request-based API."""
    return await _run_baseline(
        runtime,
        workspace,
        BaselineRunRequest(evaluator_ref, task, predict_features, verified),
    )


def valid_payload() -> dict[str, Any]:
    """Return one complete, hand-checked research artifact."""
    return {
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
        schema_version=2,
        research_sha256=research_sha256(artifacts.raw_research),
        design_sha256=design_sha256(artifacts.raw_design),
        selected_candidate_id=artifacts.selected.candidate_id,
        route="git",
        verified_at=datetime(2026, 9, 2, tzinfo=UTC),
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
    def __init__(self, authority: Any = ...) -> None:
        self.agents = FakeAgents()
        self.outputs: list[dict[str, Any]] = []
        self.registry = SimpleNamespace(contains=lambda _agent_type: True)
        self.provider = object()
        self.store = object()
        self.execution = object()
        self.baseline_authority = (
            MemoryBaselineAuthorityStore() if authority is ... else authority
        )

    def baseline_ideator_tools(self) -> object:
        return object()

    async def publish_output(self, **kwargs: Any) -> None:
        self.outputs.append(kwargs)


class MemoryBaselineAuthorityStore:
    """Test-only external authority with compare-and-exchange semantics."""

    def __init__(
        self,
        sealed: SealedBaseline | None = None,
        *,
        load_error: Exception | None = None,
        seal_error: Exception | None = None,
    ) -> None:
        self.sealed = sealed
        self.load_error = load_error
        self.seal_error = seal_error
        self.loads = 0
        self.seals = 0
        self.attestations: list[tuple[PrepareAttestation, int]] = []

    async def load(self) -> SealedBaseline | None:
        self.loads += 1
        if self.load_error is not None:
            raise self.load_error
        return self.sealed

    async def seal(
        self,
        bundle: VerifiedBaselineBundle,
        *,
        expected_generation: int | None,
    ) -> SealedBaseline:
        self.seals += 1
        if self.seal_error is not None:
            raise self.seal_error
        current = None if self.sealed is None else self.sealed.generation
        if current != expected_generation:
            raise BaselineAuthorityConflict("baseline generation changed")
        generation = 0 if current is None else current + 1
        self.sealed = SealedBaseline(generation=generation, bundle=bundle)
        return self.sealed

    async def attest_prepare(
        self,
        evidence: PrepareAttestation,
        *,
        expected_generation: int,
    ) -> SealedBaseline:
        self.attestations.append((evidence, expected_generation))
        if self.sealed is None or self.sealed.generation != expected_generation:
            raise BaselineAuthorityConflict("baseline generation changed")
        self.sealed = SealedBaseline(
            generation=expected_generation + 1,
            bundle=self.sealed.bundle,
            attestation=evidence,
        )
        return self.sealed


def authority_bundle(root: Path) -> VerifiedBaselineBundle:
    """Build exact authority bytes from one valid local artifact pair."""
    artifacts = load_baseline_artifacts(root)
    verification = valid_verification(artifacts)
    return VerifiedBaselineBundle(
        research_bytes=artifacts.raw_research,
        design_bytes=artifacts.raw_design,
        verification_bytes=verification_bytes(verification),
        verification=verification,
    )


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
async def test_terminal_research_diagnostics_are_distinct_bounded_and_redacted() -> (
    None
):
    runtime = FakeRuntime()

    await baseline._publish_terminal_research_error(
        runtime,
        BaselineResearchError(
            "invalid BASELINE_RESEARCH.json",
            ("decisions.0.decision must be 'selected' or 'rejected'", "api_key=secret"),
        ),
    )
    await baseline._publish_terminal_research_error(
        runtime,
        BaselineResearchError(
            "selected candidate has no qualifying source",
            ("OpenAlex title does not match selected source",),
        ),
    )

    first, second = (output["text"] for output in runtime.outputs)
    assert "BASELINE_SCHEMA_INVALID" in first
    assert "decisions.0.decision" in first
    assert "secret" not in first
    assert "BASELINE_EVIDENCE_INVALID" in second
    assert "OpenAlex title does not match selected source" in second


@pytest.mark.asyncio
async def test_openalex_unresolved_locator_is_published_as_warning() -> None:
    runtime = FakeRuntime()

    await baseline._publish_terminal_research_error(
        runtime,
        BaselineResearchError(
            "selected candidate has no qualifying source",
            ("OpenAlex did not resolve the paper locator",),
        ),
    )

    assert runtime.outputs[-1]["channel"] == "warning"
    assert "baseline research warning" in runtime.outputs[-1]["text"]


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
async def test_missing_authority_fails_before_baseline_agent_side_effects(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime(None)
    handoff = ScriptedHandoff(tmp_path, [writes_valid])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    with pytest.raises(BaselineAuthorityError, match="authority"):
        await prepare_baseline_design(
            runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
        )

    assert handoff.calls == []
    assert verifier.calls == []
    assert runtime.agents.reaped == []


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
async def test_forged_workspace_trio_with_empty_authority_is_live_verified(
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    write_forged_verification(tmp_path)
    authority = MemoryBaselineAuthorityStore()
    runtime = FakeRuntime(authority)
    handoff = ScriptedHandoff(tmp_path, [])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    result = await prepare_baseline_design(
        runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
    )

    assert result.verification.commit == "a" * 40
    assert len(verifier.calls) == 1
    assert authority.loads == 1
    assert authority.seals == 1
    assert handoff.calls == []
    assert runtime.agents.reaped == []


@pytest.mark.asyncio
async def test_exact_authority_restart_returns_before_default_verifier_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_artifacts(tmp_path)
    bundle = authority_bundle(tmp_path)
    (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").write_bytes(
        bundle.verification_bytes
    )
    authority = MemoryBaselineAuthorityStore(
        SealedBaseline(generation=0, bundle=bundle)
    )

    class ForbiddenAgents(FakeAgents):
        async def reap(self, agent_id: str) -> None:
            raise AssertionError(f"cache hit must not reap {agent_id}")

    runtime = FakeRuntime(authority)
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
    assert result.authority_generation == 0
    assert result.verification_bytes == bundle.verification_bytes
    assert authority.loads == 1
    assert authority.seals == 0


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
async def test_missing_verification_mirror_is_restored_exactly_from_authority(
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    bundle = authority_bundle(tmp_path)
    authority = MemoryBaselineAuthorityStore(
        SealedBaseline(generation=0, bundle=bundle)
    )

    result = await prepare_baseline_design(
        FakeRuntime(authority),
        workspace(tmp_path),
        "task",
        True,
        ScriptedHandoff(tmp_path, []),
        verifier=QueuedVerifier(tmp_path, []),
    )

    assert result.authority_generation == 0
    assert (
        tmp_path / "BASELINE_RESEARCH_VERIFICATION.json"
    ).read_bytes() == bundle.verification_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "mutate"),
    [
        (
            "BASELINE_RESEARCH.json",
            lambda root: write_artifacts(root, changed=True),
        ),
        (
            "BASELINE_DESIGN.md",
            lambda root: (root / "BASELINE_DESIGN.md").write_text(
                "# Rewritten body\n\nSelected candidate: `resnet-transfer`\n"
                "Training strategy: `partial_finetune`\n",
                encoding="utf-8",
            ),
        ),
        (
            "BASELINE_RESEARCH_VERIFICATION.json",
            lambda root: (root / "BASELINE_RESEARCH_VERIFICATION.json").write_bytes(
                verification_bytes(
                    valid_verification(load_baseline_artifacts(root)).model_copy(
                        update={
                            "attempts": (
                                VerificationAttempt(
                                    route="git",
                                    success=True,
                                    diagnostic="rewritten mirror",
                                ),
                            )
                        }
                    )
                )
            ),
        ),
    ],
)
async def test_authority_mirror_mutation_stops_before_prepare_or_live_verification(
    filename: str,
    mutate: Callable[[Path], Any],
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    bundle = authority_bundle(tmp_path)
    (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").write_bytes(
        bundle.verification_bytes
    )
    authority = MemoryBaselineAuthorityStore(
        SealedBaseline(generation=0, bundle=bundle)
    )
    mutate(tmp_path)
    verifier = QueuedVerifier(tmp_path, [])
    handoff = ScriptedHandoff(tmp_path, [])

    with pytest.raises(BaselineResearchError, match=filename):
        await prepare_baseline_design(
            FakeRuntime(authority),
            workspace(tmp_path),
            "task",
            True,
            handoff,
            verifier=verifier,
        )

    assert verifier.calls == []
    assert handoff.calls == []


@pytest.mark.asyncio
async def test_v1_workspace_mirror_is_ignored_and_live_verified(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "research_sha256": research_sha256(
                    (tmp_path / "BASELINE_RESEARCH.json").read_bytes()
                ),
                "selected_candidate_id": "resnet-transfer",
                "route": "git",
            }
        ),
        encoding="utf-8",
    )
    verifier = QueuedVerifier(tmp_path, ["valid"])
    authority = MemoryBaselineAuthorityStore()

    result = await prepare_baseline_design(
        FakeRuntime(authority),
        workspace(tmp_path),
        "task",
        True,
        ScriptedHandoff(tmp_path, []),
        verifier=verifier,
    )

    assert result.verification.schema_version == 2
    assert len(verifier.calls) == 1
    assert authority.seals == 1


@pytest.mark.asyncio
async def test_authority_outage_never_falls_back_to_matching_workspace(
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    write_forged_verification(tmp_path)
    authority = MemoryBaselineAuthorityStore(load_error=OSError("authority offline"))
    verifier = QueuedVerifier(tmp_path, [])
    handoff = ScriptedHandoff(tmp_path, [])

    with pytest.raises(BaselineAuthorityError, match="authority"):
        await prepare_baseline_design(
            FakeRuntime(authority),
            workspace(tmp_path),
            "task",
            True,
            handoff,
            verifier=verifier,
        )

    assert verifier.calls == []
    assert handoff.calls == []


@pytest.mark.asyncio
async def test_authority_load_error_is_stable_and_suppresses_adapter_details(
    tmp_path: Path,
) -> None:
    secret = "authority-load-endpoint-token-secret"
    authority = MemoryBaselineAuthorityStore(load_error=BaselineAuthorityError(secret))

    with pytest.raises(BaselineAuthorityError) as caught:
        await prepare_baseline_design(
            FakeRuntime(authority),
            workspace(tmp_path),
            "task",
            True,
            ScriptedHandoff(tmp_path, []),
            verifier=QueuedVerifier(tmp_path, []),
        )

    assert str(caught.value) == "baseline authority load failed"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert secret not in "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )


@pytest.mark.asyncio
async def test_seal_failure_leaves_no_verification_mirror(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    write_forged_verification(tmp_path)
    authority = MemoryBaselineAuthorityStore(seal_error=OSError("authority offline"))

    with pytest.raises(BaselineAuthorityError, match="authority"):
        await prepare_baseline_design(
            FakeRuntime(authority),
            workspace(tmp_path),
            "task",
            True,
            ScriptedHandoff(tmp_path, []),
            verifier=QueuedVerifier(tmp_path, ["valid"]),
        )

    assert not (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").exists()


@pytest.mark.asyncio
async def test_authority_seal_error_is_stable_and_suppresses_adapter_details(
    tmp_path: Path,
) -> None:
    secret = "authority-seal-client-key-path-secret"
    write_artifacts(tmp_path)
    authority = MemoryBaselineAuthorityStore(seal_error=BaselineAuthorityError(secret))

    with pytest.raises(BaselineAuthorityError) as caught:
        await prepare_baseline_design(
            FakeRuntime(authority),
            workspace(tmp_path),
            "task",
            True,
            ScriptedHandoff(tmp_path, []),
            verifier=QueuedVerifier(tmp_path, ["valid"]),
        )

    assert str(caught.value) == "baseline authority seal failed"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert secret not in "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )


@pytest.mark.asyncio
async def test_authority_load_cancellation_is_not_translated(tmp_path: Path) -> None:
    authority = MemoryBaselineAuthorityStore(load_error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await prepare_baseline_design(
            FakeRuntime(authority),
            workspace(tmp_path),
            "task",
            True,
            ScriptedHandoff(tmp_path, []),
            verifier=QueuedVerifier(tmp_path, []),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("generation", "has_attestation"),
    [(0, True), (1, False), (2, True)],
)
async def test_invalid_authority_lifecycle_fails_before_verifier_or_agent(
    generation: int,
    has_attestation: bool,
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    bundle = authority_bundle(tmp_path)
    attestation = (
        PrepareAttestation(
            research_sha256=bundle.verification.research_sha256,
            design_sha256=bundle.verification.design_sha256,
            baseline_commit="b" * 40,
            evaluator_ref="sha256:" + "e" * 64,
            evidence_ref="sha256:" + "d" * 64,
        )
        if has_attestation
        else None
    )
    authority = MemoryBaselineAuthorityStore(
        SealedBaseline(
            generation=generation,
            bundle=bundle,
            attestation=attestation,
        )
    )
    handoff = ScriptedHandoff(tmp_path, [])
    verifier = QueuedVerifier(tmp_path, [])

    with pytest.raises(BaselineAuthorityError, match="lifecycle"):
        await prepare_baseline_design(
            FakeRuntime(authority),
            workspace(tmp_path),
            "task",
            True,
            handoff,
            verifier=verifier,
        )

    assert authority.loads == 1
    assert authority.seals == 0
    assert verifier.calls == []
    assert handoff.calls == []
    assert not (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").exists()


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
async def test_reap_cancellation_is_observed_without_replacing_success(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    class CancelledReapAgents(FakeAgents):
        async def reap(self, agent_id: str) -> None:
            self.reaped.append(agent_id)
            raise asyncio.CancelledError()

    runtime = FakeRuntime()
    runtime.agents = CancelledReapAgents()
    handoff = ScriptedHandoff(tmp_path, [writes_valid])
    verifier = QueuedVerifier(tmp_path, ["valid"])

    with caplog.at_level("WARNING", logger=baseline.__name__):
        result = await prepare_baseline_design(
            runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
        )
        await asyncio.sleep(0)

    assert result.verification.route == "git"
    assert runtime.agents.reaped == ["baseline_ideator"]
    assert "baseline ideator reap was cancelled" in caplog.text


@pytest.mark.asyncio
async def test_reap_cancellation_does_not_replace_terminal_research_error(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    class CancelledReapAgents(FakeAgents):
        async def reap(self, agent_id: str) -> None:
            self.reaped.append(agent_id)
            raise asyncio.CancelledError()

    runtime = FakeRuntime()
    runtime.agents = CancelledReapAgents()
    handoff = ScriptedHandoff(tmp_path, [writes_valid, writes_valid])
    verifier = QueuedVerifier(
        tmp_path,
        [
            BaselineResearchError("bad source", ["clone failed"]),
            BaselineResearchError("still bad", ["OpenAlex unresolved"]),
        ],
    )

    with caplog.at_level("WARNING", logger=baseline.__name__):
        with pytest.raises(BaselineResearchError, match="still bad"):
            await prepare_baseline_design(
                runtime, workspace(tmp_path), "task", True, handoff, verifier=verifier
            )
        await asyncio.sleep(0)

    assert runtime.agents.reaped == ["baseline_ideator"]
    assert "baseline ideator reap was cancelled" in caplog.text


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
            schema_version=2,
            research_sha256=research_sha256(artifacts.raw_research),
            design_sha256=design_sha256(artifacts.raw_design),
            selected_candidate_id=artifacts.selected.candidate_id,
            route="openalex",
            verified_at=datetime(2026, 9, 2, tzinfo=UTC),
            paper_locator=artifacts.selected.paper_locator,
            openalex_id="W2741809807",
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
    canonical = verification_bytes(verification)
    (root / "BASELINE_RESEARCH_VERIFICATION.json").write_bytes(canonical)
    return VerifiedBaseline(
        artifacts=artifacts,
        verification=verification,
        verification_bytes=canonical,
        authority_generation=0,
    )


def authority_for_verified(verified: VerifiedBaseline) -> MemoryBaselineAuthorityStore:
    bundle = VerifiedBaselineBundle(
        research_bytes=verified.artifacts.raw_research,
        design_bytes=verified.artifacts.raw_design,
        verification_bytes=verified.verification_bytes,
        verification=verified.verification,
    )
    return MemoryBaselineAuthorityStore(
        SealedBaseline(generation=verified.authority_generation, bundle=bundle)
    )


def completed_prepare_result() -> PrepareResult:
    """Return one complete trusted-scoring result with literal evidence refs."""
    return PrepareResult(
        evaluator_ref="sha256:" + "e" * 64,
        metric=0.71,
        commit="b" * 40,
        predictions_ref="sha256:" + "1" * 64,
        evidence_ref="sha256:" + "d" * 64,
        report_ref="sha256:" + "2" * 64,
    )


def test_verified_carrier_rejects_mismatched_canonical_verification_bytes(
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    verification = valid_verification(artifacts)
    different = verification.model_copy(update={"commit": "b" * 40})

    with pytest.raises(BaselineResearchError, match="verification bytes"):
        VerifiedBaseline(
            artifacts=artifacts,
            verification=verification,
            verification_bytes=verification_bytes(different),
            authority_generation=0,
        )


class PrepareRuntime:
    def __init__(self, authority: Any) -> None:
        self.registry = SimpleNamespace(contains=lambda _agent_type: True)
        self.provider = object()
        self.store = SimpleNamespace(put_text=self._put_text)
        self.execution = object()
        self.tree = SimpleNamespace(to_dict=lambda: {"tree": "frozen"})
        self.agents = object()
        self.evaluator = object()
        self.git = object()
        self.baseline_authority = authority
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
    runtime = PrepareRuntime(authority_for_verified(verified))
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
            paper_locator=verified.artifacts.selected.paper_locator,
            openalex_id="W2741809807",
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
        ("BASELINE_RESEARCH.json", "{}", "BASELINE_RESEARCH.json"),
        (
            "BASELINE_DESIGN.md",
            "Selected candidate: `resnet-transfer`\nTraining strategy: `classical`\n",
            "BASELINE_DESIGN.md",
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
@pytest.mark.parametrize("difference", ["malformed", "route", "revision", "attempt"])
async def test_run_baseline_rejects_changed_verification_without_prepare_side_effects(
    difference: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = verified_fixture(tmp_path)
    verification_path = tmp_path / "BASELINE_RESEARCH_VERIFICATION.json"
    if difference == "malformed":
        verification_path.write_text("{}", encoding="utf-8")
    else:
        write_different_verification(tmp_path, verified, difference)

    await assert_rejected_without_prepare_side_effects(
        monkeypatch,
        tmp_path,
        verified,
        match="BASELINE_RESEARCH_VERIFICATION.json",
    )


@pytest.mark.asyncio
async def test_run_baseline_restores_missing_verification_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = verified_fixture(tmp_path)
    (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").unlink()
    registered: list[str] = []

    monkeypatch.setattr(
        baseline,
        "_register_prepare_agent",
        lambda *_args: registered.append("prepare"),
    )
    expected = completed_prepare_result()

    async def prepared(**_kwargs: Any) -> PrepareResult:
        return expected

    monkeypatch.setattr(baseline, "run_prepare_plan", prepared)
    result = await run_baseline(
        PrepareRuntime(authority_for_verified(verified)),
        workspace(tmp_path),
        expected.evaluator_ref,
        "task",
        None,
        verified,
    )

    assert result == expected
    assert registered == ["prepare"]
    assert (
        tmp_path / "BASELINE_RESEARCH_VERIFICATION.json"
    ).read_bytes() == verified.verification_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "revision"),
    [
        ("git", "a" * 40),
        ("openalex", "W2741809807"),
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

    class RecordingAuthority(MemoryBaselineAuthorityStore):
        async def load(self) -> SealedBaseline | None:
            order.append("checked")
            return await super().load()

    def registered(*_args: Any) -> None:
        order.append("registered")

    expected = completed_prepare_result()

    async def fake_run_prepare_plan(**kwargs: Any) -> PrepareResult:
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(baseline, "_register_prepare_agent", registered)
    monkeypatch.setattr(baseline, "run_prepare_plan", fake_run_prepare_plan)

    result = await run_baseline(
        PrepareRuntime(RecordingAuthority(authority_for_verified(verified).sealed)),
        workspace(tmp_path),
        expected.evaluator_ref,
        "task",
        None,
        verified,
    )

    assert result == expected
    assert order == ["checked", "registered", "checked"]
    assert callable(captured["assert_baseline"])
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


@pytest.mark.asyncio
async def test_run_baseline_attests_only_the_complete_trusted_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = verified_fixture(tmp_path)
    authority = authority_for_verified(verified)
    expected = completed_prepare_result()

    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_args: None)

    async def scored(**_kwargs: Any) -> PrepareResult:
        return expected

    monkeypatch.setattr(baseline, "run_prepare_plan", scored)

    result = await run_baseline(
        PrepareRuntime(authority),
        workspace(tmp_path),
        expected.evaluator_ref,
        "task",
        None,
        verified,
    )

    expected_attestation = PrepareAttestation(
        research_sha256=verified.verification.research_sha256,
        design_sha256=verified.verification.design_sha256,
        baseline_commit="b" * 40,
        evaluator_ref="sha256:" + "e" * 64,
        evidence_ref="sha256:" + "d" * 64,
    )
    assert result == expected
    assert authority.attestations == [(expected_attestation, 0)]
    assert authority.sealed == SealedBaseline(
        generation=1,
        bundle=authority_bundle(tmp_path),
        attestation=expected_attestation,
    )


@pytest.mark.asyncio
async def test_run_baseline_rechecks_artifacts_after_scoring_before_attestation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = verified_fixture(tmp_path)
    authority = authority_for_verified(verified)

    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_args: None)

    async def scored_then_mutated(**_kwargs: Any) -> PrepareResult:
        (tmp_path / "BASELINE_DESIGN.md").write_text(
            "Selected candidate: `resnet-transfer`\n"
            "Training strategy: `classical`\n",
            encoding="utf-8",
        )
        return completed_prepare_result()

    monkeypatch.setattr(baseline, "run_prepare_plan", scored_then_mutated)

    with pytest.raises(BaselineResearchError, match="BASELINE_DESIGN.md"):
        await run_baseline(
            PrepareRuntime(authority),
            workspace(tmp_path),
            completed_prepare_result().evaluator_ref,
            "task",
            None,
            verified,
        )

    assert authority.attestations == []


class InvalidAttestationAuthority(MemoryBaselineAuthorityStore):
    def __init__(self, sealed: SealedBaseline, difference: str) -> None:
        super().__init__(sealed)
        self.difference = difference

    async def attest_prepare(
        self,
        evidence: PrepareAttestation,
        *,
        expected_generation: int,
    ) -> SealedBaseline:
        self.attestations.append((evidence, expected_generation))
        assert self.sealed is not None
        if self.difference == "generation":
            return SealedBaseline(
                generation=expected_generation,
                bundle=self.sealed.bundle,
                attestation=evidence,
            )
        if self.difference == "bundle":
            different_verification = self.sealed.bundle.verification.model_copy(
                update={"commit": "c" * 40}
            )
            different_bundle = VerifiedBaselineBundle(
                research_bytes=self.sealed.bundle.research_bytes,
                design_bytes=self.sealed.bundle.design_bytes,
                verification_bytes=verification_bytes(different_verification),
                verification=different_verification,
            )
            return SealedBaseline(
                generation=expected_generation + 1,
                bundle=different_bundle,
                attestation=evidence,
            )
        assert self.difference == "attestation"
        different_attestation = PrepareAttestation(
            research_sha256=evidence.research_sha256,
            design_sha256=evidence.design_sha256,
            baseline_commit="c" * 40,
            evaluator_ref=evidence.evaluator_ref,
            evidence_ref=evidence.evidence_ref,
        )
        return SealedBaseline(
            generation=expected_generation + 1,
            bundle=self.sealed.bundle,
            attestation=different_attestation,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["generation", "bundle", "attestation"])
async def test_run_baseline_rejects_inexact_completion_attestation(
    difference: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified = verified_fixture(tmp_path)
    sealed = authority_for_verified(verified).sealed
    assert sealed is not None
    authority = InvalidAttestationAuthority(sealed, difference)
    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_args: None)

    async def scored(**_kwargs: Any) -> PrepareResult:
        return completed_prepare_result()

    monkeypatch.setattr(baseline, "run_prepare_plan", scored)

    with pytest.raises(BaselineAuthorityError, match="completion attestation"):
        await run_baseline(
            PrepareRuntime(authority),
            workspace(tmp_path),
            completed_prepare_result().evaluator_ref,
            "task",
            None,
            verified,
        )


@pytest.mark.asyncio
async def test_run_baseline_wraps_completion_attestation_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = verified_fixture(tmp_path)
    secret = "authority-attestation-token-secret"

    class OutageAuthority(MemoryBaselineAuthorityStore):
        async def attest_prepare(self, evidence, *, expected_generation: int):
            del evidence, expected_generation
            raise BaselineAuthorityError(secret)

    sealed = authority_for_verified(verified).sealed
    assert sealed is not None
    authority = OutageAuthority(sealed)
    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_args: None)

    async def scored(**_kwargs: Any) -> PrepareResult:
        return completed_prepare_result()

    monkeypatch.setattr(baseline, "run_prepare_plan", scored)

    with pytest.raises(BaselineAuthorityError) as caught:
        await run_baseline(
            PrepareRuntime(authority),
            workspace(tmp_path),
            completed_prepare_result().evaluator_ref,
            "task",
            None,
            verified,
        )

    assert str(caught.value) == "baseline authority completion attestation failed"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert secret not in "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )


@pytest.mark.asyncio
async def test_authority_attestation_cancellation_is_not_translated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = verified_fixture(tmp_path)

    class CancelledAuthority(MemoryBaselineAuthorityStore):
        async def attest_prepare(self, evidence, *, expected_generation: int):
            del evidence, expected_generation
            raise asyncio.CancelledError()

    sealed = authority_for_verified(verified).sealed
    assert sealed is not None
    authority = CancelledAuthority(sealed)
    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_args: None)

    async def scored(**_kwargs: Any) -> PrepareResult:
        return completed_prepare_result()

    monkeypatch.setattr(baseline, "run_prepare_plan", scored)

    with pytest.raises(asyncio.CancelledError):
        await run_baseline(
            PrepareRuntime(authority),
            workspace(tmp_path),
            completed_prepare_result().evaluator_ref,
            "task",
            None,
            verified,
        )


def _verified_at_generation(
    verified: VerifiedBaseline, generation: int
) -> VerifiedBaseline:
    return VerifiedBaseline(
        artifacts=verified.artifacts,
        verification=verified.verification,
        verification_bytes=verified.verification_bytes,
        authority_generation=generation,
    )


def _completion_attestation(
    verified: VerifiedBaseline, result: PrepareResult
) -> PrepareAttestation:
    return PrepareAttestation(
        research_sha256=verified.verification.research_sha256,
        design_sha256=verified.verification.design_sha256,
        baseline_commit=result.commit,
        evaluator_ref=result.evaluator_ref,
        evidence_ref=result.evidence_ref,
    )


@pytest.mark.asyncio
async def test_run_baseline_idempotently_accepts_generation_one_after_tree_loss(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = _verified_at_generation(verified_fixture(tmp_path), 1)
    expected = completed_prepare_result()
    sealed = SealedBaseline(
        generation=1,
        bundle=authority_bundle(tmp_path),
        attestation=_completion_attestation(verified, expected),
    )
    authority = MemoryBaselineAuthorityStore(sealed)
    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_args: None)

    async def rescored(**_kwargs: Any) -> PrepareResult:
        return expected

    monkeypatch.setattr(baseline, "run_prepare_plan", rescored)

    result = await run_baseline(
        PrepareRuntime(authority),
        workspace(tmp_path),
        expected.evaluator_ref,
        "task",
        None,
        verified,
    )

    assert result == expected
    assert authority.attestations == []
    assert authority.sealed == sealed


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["commit", "evaluator_ref", "evidence_ref"])
async def test_run_baseline_rejects_generation_one_attestation_mismatch(
    difference: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified = _verified_at_generation(verified_fixture(tmp_path), 1)
    prior_result = completed_prepare_result()
    sealed = SealedBaseline(
        generation=1,
        bundle=authority_bundle(tmp_path),
        attestation=_completion_attestation(verified, prior_result),
    )
    authority = MemoryBaselineAuthorityStore(sealed)
    changes = {
        "commit": "c" * 40,
        "evaluator_ref": "sha256:" + "c" * 64,
        "evidence_ref": "sha256:" + "f" * 64,
    }
    rescored_result = prior_result.model_copy(update={difference: changes[difference]})
    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_args: None)

    async def rescored(**_kwargs: Any) -> PrepareResult:
        return rescored_result

    monkeypatch.setattr(baseline, "run_prepare_plan", rescored)

    with pytest.raises(BaselineAuthorityError, match="existing completion attestation"):
        await run_baseline(
            PrepareRuntime(authority),
            workspace(tmp_path),
            rescored_result.evaluator_ref,
            "task",
            None,
            verified,
        )

    assert authority.attestations == []
    assert authority.sealed == sealed


@pytest.mark.asyncio
async def test_run_baseline_rejects_completion_generation_above_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verified = _verified_at_generation(verified_fixture(tmp_path), 2)
    expected = completed_prepare_result()
    sealed = SealedBaseline(
        generation=2,
        bundle=authority_bundle(tmp_path),
        attestation=_completion_attestation(verified, expected),
    )
    authority = MemoryBaselineAuthorityStore(sealed)
    monkeypatch.setattr(baseline, "_register_prepare_agent", lambda *_args: None)
    plan_calls: list[str] = []

    async def rescored(**_kwargs: Any) -> PrepareResult:
        plan_calls.append("scored")
        return expected

    monkeypatch.setattr(baseline, "run_prepare_plan", rescored)

    runtime = PrepareRuntime(authority)
    with pytest.raises(BaselineAuthorityError, match="lifecycle"):
        await run_baseline(
            runtime,
            workspace(tmp_path),
            expected.evaluator_ref,
            "task",
            None,
            verified,
        )

    assert authority.attestations == []
    assert authority.sealed == sealed
    assert runtime.outputs == []
    assert runtime.snapshots == []
    assert plan_calls == []
