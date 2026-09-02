"""Contract tests for the baseline research artifacts."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.research.prepare.baseline_research import (
    BaselineResearch,
    load_baseline_artifacts,
)


def valid_payload() -> dict:
    """Return a minimal valid baseline research document."""
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


def write_artifacts(
    root: Path, payload: dict | None = None, design: str | None = None
) -> None:
    """Write research and design fixtures."""
    (root / "BASELINE_RESEARCH.json").write_text(
        json.dumps(payload or valid_payload()), encoding="utf-8"
    )
    (root / "BASELINE_DESIGN.md").write_text(
        design
        or "# Baseline\n\nSelected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n",
        encoding="utf-8",
    )


def test_loads_matching_research_and_design(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    assert artifacts.selected.candidate_id == "resnet-transfer"
    assert artifacts.design.training_strategy == "partial_finetune"
    assert artifacts.raw_research == (tmp_path / "BASELINE_RESEARCH.json").read_bytes()


@pytest.mark.parametrize(
    ("regime", "strategy"),
    [("unknown", "train_from_scratch"), ("tiny", "train_from_scratch")],
)
def test_rejects_unsafe_scratch_policy(regime: str, strategy: str) -> None:
    payload = valid_payload()
    payload["dataset"]["regime"] = regime
    payload["dataset"]["recommended_strategy"] = strategy
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_scratch_requires_local_and_comparable_source_evidence() -> None:
    payload = valid_payload()
    payload["dataset"].update(
        regime="adequate",
        recommended_strategy="train_from_scratch",
        evidence=["eda:labels.csv: enough independent samples"],
    )
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)
    payload["dataset"]["evidence"].append("source:resnet-transfer: comparable scale")
    assert BaselineResearch.model_validate(payload).dataset.recommended_strategy == (
        "train_from_scratch"
    )


def test_small_tabular_classical_is_allowed() -> None:
    payload = valid_payload()
    payload["dataset"].update(
        modality="tabular",
        regime="small",
        recommended_strategy="classical",
        input_scale="480 rows and 12 columns",
    )
    assert BaselineResearch.model_validate(payload).dataset.modality == "tabular"


def test_data_contract_evidence_is_accepted() -> None:
    payload = valid_payload()
    payload["dataset"]["evidence"] = ["data_contract:labels.csv: grouped labels"]
    assert (
        BaselineResearch.model_validate(payload)
        .dataset.evidence[0]
        .startswith("data_contract:")
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["candidates"].append(p["candidates"][0].copy()),
        lambda p: p["decisions"].pop(),
        lambda p: p.update(selected_candidate_id="linear-probe"),
    ],
)
def test_rejects_inconsistent_candidates_and_decisions(change) -> None:
    payload = valid_payload()
    change(payload)
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_one_candidate_needs_multiple_queries_and_limitation() -> None:
    payload = valid_payload()
    payload["candidates"] = payload["candidates"][:1]
    payload["decisions"] = payload["decisions"][:1]
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)
    payload["search_queries"] = ["query one", "query two"]
    payload["limitations"] = ["Only one directly comparable source was found."]
    assert BaselineResearch.model_validate(payload).selected_candidate_id == (
        "resnet-transfer"
    )


def test_design_markers_are_exact_and_cross_file_consistent(tmp_path: Path) -> None:
    write_artifacts(tmp_path, design="Selected candidate: `linear-probe`\n")
    with pytest.raises(Exception, match="exactly once"):
        load_baseline_artifacts(tmp_path)
    write_artifacts(
        tmp_path,
        design=(
            "Selected candidate: `resnet-transfer`\n"
            "Selected candidate: `resnet-transfer`\n"
            "Training strategy: `partial_finetune`\n"
        ),
    )
    with pytest.raises(Exception, match="exactly once"):
        load_baseline_artifacts(tmp_path)


@pytest.mark.parametrize(
    "url",
    ["https://user:password@example.com/repo.git", "https://user@example.com/repo.git"],
)
def test_rejects_url_credentials(url: str) -> None:
    payload = valid_payload()
    payload["candidates"][0]["repository_url"] = url
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)
