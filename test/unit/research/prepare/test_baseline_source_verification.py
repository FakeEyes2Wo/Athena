import json
from pathlib import Path

import pytest

from athena.research.literature.paper_source.openalex import OpenAlexWork
from athena.research.prepare.baseline_research import (
    BaselineResearchError,
    load_baseline_artifacts,
)
from athena.research.prepare.source_verification import (
    BaselineSourceVerifier,
    GitCloneEvidence,
    titles_match,
)


def _payload(*, repository_url: str | None = "https://github.com/org/repo.git") -> dict:
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
            "evidence": ["eda:EDA_REPORT_LABELS.md: grouped labels"],
            "rationale": "Transfer learning fits the grouped image scale.",
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
                "relevance": "A transfer baseline for small image datasets.",
            },
            {
                "candidate_id": "linear-probe",
                "title": "PyTorch transfer learning tutorial",
                "method": "frozen features with a linear head",
                "source_url": "https://docs.pytorch.org/tutorials/beginner/transfer_learning_tutorial.html",
                "source_kind": "technical_reference",
                "paper_locator": None,
                "repository_url": "https://github.com/pytorch/tutorials.git",
                "publication_year": None,
                "claimed_citation_count": None,
                "relevance": "A conservative alternative.",
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
        "search_queries": ["image classification transfer baseline"],
        "limitations": [],
    }


def valid_artifacts(
    tmp_path: Path, *, repository_url: str | None = "https://github.com/org/repo.git"
):
    (tmp_path / "BASELINE_RESEARCH.json").write_text(
        json.dumps(_payload(repository_url=repository_url)), encoding="utf-8"
    )
    (tmp_path / "BASELINE_DESIGN.md").write_text(
        "# Baseline\n\nSelected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n",
        encoding="utf-8",
    )
    return load_baseline_artifacts(tmp_path)


class FakeGit:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = []

    async def verify(self, url: str):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.result


class FakeOpenAlex:
    def __init__(self, work=None, error: Exception | None = None):
        self.work = work
        self.error = error
        self.calls = []

    async def fetch_work(self, locator: str):
        self.calls.append(locator)
        if self.error:
            raise self.error
        return self.work


@pytest.mark.asyncio
async def test_repository_success_does_not_require_openalex(tmp_path: Path) -> None:
    artifacts = valid_artifacts(tmp_path)
    git = FakeGit(GitCloneEvidence("https://github.com/org/repo.git", "b" * 40))
    openalex = FakeOpenAlex(error=AssertionError("OpenAlex must not be called"))

    result = await BaselineSourceVerifier(git=git, openalex=openalex).verify(artifacts)

    assert result.route == "git"
    assert result.commit == "b" * 40
    assert openalex.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("citations", "passes"), [(99, False), (100, True)])
async def test_openalex_threshold_is_inclusive(
    tmp_path: Path, citations: int, passes: bool
) -> None:
    artifacts = valid_artifacts(tmp_path, repository_url=None)
    openalex = FakeOpenAlex(
        work=OpenAlexWork(
            openalex_id="W123",
            title=artifacts.selected.title,
            publication_year=2016,
            cited_by_count=citations,
        )
    )

    verifier = BaselineSourceVerifier(git=FakeGit(), openalex=openalex)
    if passes:
        assert (await verifier.verify(artifacts)).route == "openalex"
    else:
        with pytest.raises(BaselineResearchError, match="no qualifying source"):
            await verifier.verify(artifacts)


def test_titles_match_is_normalized_but_short_titles_are_not_fuzzy() -> None:
    assert titles_match(
        "Deep Residual-Learning for Image Recognition",
        "deep residual learning for image recognition",
    )
    assert not titles_match("Short title", "Short title changed")
    assert not titles_match("A" * 20, "B" * 20)


@pytest.mark.asyncio
async def test_git_failure_falls_back_to_qualifying_openalex(tmp_path: Path) -> None:
    artifacts = valid_artifacts(tmp_path)
    git = FakeGit(
        error=BaselineResearchError("Git source verification failed", ["clone failed"])
    )
    openalex = FakeOpenAlex(
        work=OpenAlexWork(
            openalex_id="W123", title=artifacts.selected.title, cited_by_count=137
        )
    )

    result = await BaselineSourceVerifier(git=git, openalex=openalex).verify(artifacts)

    assert result.route == "openalex"
    assert [attempt.route for attempt in result.attempts] == ["git", "openalex"]
    assert result.cited_by_count == 137


@pytest.mark.asyncio
async def test_title_mismatch_and_openalex_outage_are_bounded_diagnostics(
    tmp_path: Path,
) -> None:
    artifacts = valid_artifacts(tmp_path, repository_url=None)
    openalex = FakeOpenAlex(
        work=OpenAlexWork(
            openalex_id="W123", title="Different paper", cited_by_count=100
        )
    )

    with pytest.raises(BaselineResearchError) as caught:
        await BaselineSourceVerifier(git=FakeGit(), openalex=openalex).verify(artifacts)

    assert "no qualifying source" in str(caught.value)
    assert any("title" in item.lower() for item in caught.value.diagnostics)
