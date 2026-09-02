import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from athena.research.literature.paper_source.openalex import (
    OpenAlexClient,
    OpenAlexWork,
)
from athena.research.prepare.baseline_research import (
    BaselineArtifacts,
    BaselineResearchError,
    load_baseline_artifacts,
)
from athena.research.prepare.source_verification import (
    BaselineSourceVerifier,
    GitCloneEvidence,
    build_default_source_verifier,
    titles_match,
)


class FakeGit:
    def __init__(
        self,
        result: GitCloneEvidence | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.urls: list[str] = []

    async def verify(self, repository_url: str) -> GitCloneEvidence:
        self.urls.append(repository_url)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise BaselineResearchError("Git source verification failed", ["no clone"])
        return self.result


class FakeOpenAlex:
    def __init__(
        self,
        work: OpenAlexWork | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.work = work
        self.error = error
        self.locators: list[str] = []

    async def fetch_work(self, locator: str) -> OpenAlexWork | None:
        self.locators.append(locator)
        if self.error is not None:
            raise self.error
        return self.work


def _valid_payload() -> dict:
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
            "rationale": "Grouped labels are limited relative to pretrained vision capacity.",
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
                "source_url": "https://docs.pytorch.org/tutorials/beginner/transfer_learning_tutorial.html",
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


@pytest.fixture
def valid_artifacts(tmp_path: Path) -> BaselineArtifacts:
    (tmp_path / "BASELINE_RESEARCH.json").write_text(
        json.dumps(_valid_payload()), encoding="utf-8"
    )
    (tmp_path / "BASELINE_DESIGN.md").write_text(
        "Selected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n",
        encoding="utf-8",
    )
    return load_baseline_artifacts(tmp_path)


def _work(artifacts: BaselineArtifacts, citations: int = 100) -> OpenAlexWork:
    return OpenAlexWork(
        openalex_id="W123",
        title=artifacts.selected.title,
        publication_year=2016,
        cited_by_count=citations,
    )


@pytest.mark.asyncio
async def test_repository_success_does_not_require_openalex(
    valid_artifacts: BaselineArtifacts,
) -> None:
    git = FakeGit(GitCloneEvidence("https://github.com/org/repo.git", "b" * 40))
    openalex = FakeOpenAlex(error=AssertionError("OpenAlex must not be called"))
    verified_at = datetime(2026, 9, 2, tzinfo=timezone.utc)

    result = await BaselineSourceVerifier(
        git=git, openalex=openalex, now=lambda: verified_at
    ).verify(valid_artifacts)

    assert result.route == "git"
    assert result.repository_url == "https://github.com/org/repo.git"
    assert result.commit == "b" * 40
    assert result.openalex_id is None
    assert result.title is None
    assert result.publication_year is None
    assert result.cited_by_count is None
    assert result.verified_at == verified_at
    assert openalex.locators == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("citations", "passes"), [(99, False), (100, True)])
async def test_openalex_threshold_is_inclusive(
    valid_artifacts: BaselineArtifacts, citations: int, passes: bool
) -> None:
    valid_artifacts.research.candidates[0].repository_url = None
    openalex = FakeOpenAlex(work=_work(valid_artifacts, citations))
    verifier = BaselineSourceVerifier(git=FakeGit(), openalex=openalex)

    if passes:
        result = await verifier.verify(valid_artifacts)
        assert result.route == "openalex"
        assert result.openalex_id == "W123"
        assert result.title == valid_artifacts.selected.title
        assert result.publication_year == 2016
        assert result.cited_by_count == citations
        assert result.repository_url is None
        assert result.commit is None
    else:
        with pytest.raises(BaselineResearchError, match="no qualifying source"):
            await verifier.verify(valid_artifacts)


@pytest.mark.asyncio
async def test_openalex_rejects_a_mismatched_title(
    valid_artifacts: BaselineArtifacts,
) -> None:
    valid_artifacts.research.candidates[0].repository_url = None
    openalex = FakeOpenAlex(
        work=OpenAlexWork(
            openalex_id="W123",
            title="An unrelated paper about another subject entirely",
            publication_year=2016,
            cited_by_count=1000,
        )
    )

    with pytest.raises(BaselineResearchError, match="no qualifying source") as caught:
        await BaselineSourceVerifier(git=FakeGit(), openalex=openalex).verify(
            valid_artifacts
        )

    assert caught.value.diagnostics == (
        "OpenAlex title does not match selected source",
    )


@pytest.mark.asyncio
async def test_openalex_rejects_an_unresolved_locator(
    valid_artifacts: BaselineArtifacts,
) -> None:
    valid_artifacts.research.candidates[0].repository_url = None

    with pytest.raises(BaselineResearchError, match="no qualifying source") as caught:
        await BaselineSourceVerifier(git=FakeGit(), openalex=FakeOpenAlex()).verify(
            valid_artifacts
        )

    assert caught.value.diagnostics == ("OpenAlex did not resolve the paper locator",)


@pytest.mark.asyncio
async def test_openalex_outage_is_a_bounded_failed_attempt(
    valid_artifacts: BaselineArtifacts,
) -> None:
    valid_artifacts.research.candidates[0].repository_url = None

    with pytest.raises(BaselineResearchError, match="no qualifying source") as caught:
        await BaselineSourceVerifier(
            git=FakeGit(), openalex=FakeOpenAlex(error=OSError("OpenAlex unavailable"))
        ).verify(valid_artifacts)

    assert caught.value.diagnostics == ("OpenAlex lookup failed: OpenAlex unavailable",)


@pytest.mark.asyncio
async def test_git_failure_falls_back_to_qualifying_openalex(
    valid_artifacts: BaselineArtifacts,
) -> None:
    git = FakeGit(
        error=BaselineResearchError("Git source verification failed", ["timeout"])
    )
    result = await BaselineSourceVerifier(
        git=git, openalex=FakeOpenAlex(_work(valid_artifacts))
    ).verify(valid_artifacts)

    assert result.route == "openalex"
    assert [attempt.model_dump() for attempt in result.attempts] == [
        {"route": "git", "success": False, "diagnostic": "timeout"},
        {
            "route": "openalex",
            "success": True,
            "diagnostic": "authority threshold verified",
        },
    ]


@pytest.mark.asyncio
async def test_both_routes_record_only_two_bounded_attempts(
    valid_artifacts: BaselineArtifacts,
) -> None:
    git = FakeGit(
        error=BaselineResearchError("Git source verification failed", ["timeout"])
    )
    openalex = FakeOpenAlex(error=OSError("service unavailable"))

    with pytest.raises(BaselineResearchError, match="no qualifying source") as caught:
        await BaselineSourceVerifier(git=git, openalex=openalex).verify(valid_artifacts)

    assert caught.value.diagnostics == (
        "timeout",
        "OpenAlex lookup failed: service unavailable",
    )
    assert git.urls == ["https://github.com/pytorch/vision.git"]
    assert openalex.locators == ["doi:10.1109/CVPR.2016.90"]


@pytest.mark.asyncio
async def test_agent_claimed_citations_do_not_qualify(
    valid_artifacts: BaselineArtifacts,
) -> None:
    valid_artifacts.research.candidates[0].repository_url = None
    valid_artifacts.research.candidates[0].claimed_citation_count = 100000

    with pytest.raises(BaselineResearchError, match="no qualifying source") as caught:
        await BaselineSourceVerifier(
            git=FakeGit(), openalex=FakeOpenAlex(_work(valid_artifacts, 0))
        ).verify(valid_artifacts)

    assert caught.value.diagnostics == ("OpenAlex citation count 0 is below 100",)


@pytest.mark.parametrize(
    ("reported", "resolved", "expected"),
    [
        ("A Study: Full-Width\uff21", "a study full width a", True),
        (
            "A sufficiently long reported title",
            "A sufficiently long reported titles",
            True,
        ),
        ("abcdefghijklmnopqrst", "abcdefghijklmnopqrxy", True),
        ("Short title", "Short title revised", False),
    ],
)
def test_titles_match_is_deterministic(
    reported: str, resolved: str, expected: bool
) -> None:
    assert titles_match(reported, resolved) is expected


def test_default_verifier_uses_free_openalex_metadata_client() -> None:
    verifier = build_default_source_verifier()

    assert isinstance(verifier.openalex, OpenAlexClient)
    assert verifier.openalex.has_api_key is False
