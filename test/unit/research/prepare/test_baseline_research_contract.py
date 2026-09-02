import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.research.prepare.baseline_research import (
    BaselineResearch,
    BaselineResearchError,
    BaselineVerification,
    VerifiedBaseline,
    assert_verified_files,
    load_baseline_artifacts,
    load_cached_verified_baseline,
    research_sha256,
    write_verification,
)


def valid_payload() -> dict:
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
    (root / "BASELINE_RESEARCH.json").write_text(
        json.dumps(payload or valid_payload()), encoding="utf-8"
    )
    (root / "BASELINE_DESIGN.md").write_text(
        design
        or "# Baseline\n\nSelected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n",
        encoding="utf-8",
    )


def verification_for(root: Path, **changes: object) -> BaselineVerification:
    artifacts = load_baseline_artifacts(root)
    values: dict[str, object] = {
        "research_sha256": research_sha256(artifacts.raw_research),
        "selected_candidate_id": artifacts.selected.candidate_id,
        "route": "git",
        "verified_at": datetime.now(timezone.utc),
        "attempts": [],
    }
    values.update(changes)
    return BaselineVerification.model_validate(values)


def test_loads_matching_research_and_design(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    assert artifacts.selected.candidate_id == "resnet-transfer"
    assert artifacts.design.training_strategy == "partial_finetune"


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


def test_accepts_scratch_only_with_local_and_comparable_source_evidence() -> None:
    payload = valid_payload()
    payload["dataset"].update(
        {
            "regime": "adequate",
            "recommended_strategy": "train_from_scratch",
            "evidence": [
                "calculation: effective units exceed source scale",
                "source:resnet-transfer: comparable image classification scale",
            ],
        }
    )
    assert BaselineResearch.model_validate(payload).dataset.regime == "adequate"

    payload["dataset"]["evidence"] = ["eda: local data inspection"]
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_accepts_classical_strategy_for_small_tabular_data() -> None:
    payload = valid_payload()
    payload["dataset"].update(
        {
            "modality": "tabular",
            "regime": "small",
            "recommended_strategy": "classical",
            "input_scale": "120 grouped rows",
        }
    )
    assert (
        BaselineResearch.model_validate(payload).dataset.recommended_strategy
        == "classical"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["candidates"].append(dict(p["candidates"][0])),
        lambda p: p.update(selected_candidate_id="linear-probe"),
        lambda p: p["decisions"].append(
            {"candidate_id": "unknown", "decision": "rejected", "reason": "no"}
        ),
    ],
)
def test_rejects_cross_candidate_contract_errors(mutate) -> None:
    payload = valid_payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_one_candidate_requires_two_queries_and_limitation() -> None:
    payload = valid_payload()
    payload["candidates"] = payload["candidates"][:1]
    payload["decisions"] = payload["decisions"][:1]
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)

    payload["search_queries"] = ["one", "two"]
    payload["limitations"] = ["only one applicable source found"]
    assert len(BaselineResearch.model_validate(payload).candidates) == 1


@pytest.mark.parametrize(
    "design",
    [
        "# x\nSelected candidate: `resnet-transfer`\n",
        "# x\nSelected candidate: `resnet-transfer`\nSelected candidate: `resnet-transfer`\nTraining strategy: `partial_finetune`\n",
        "# x\nSelected candidate: `linear-probe`\nTraining strategy: `partial_finetune`\n",
        "# x\nSelected candidate: `resnet-transfer`\nTraining strategy: `classical`\n",
    ],
)
def test_requires_exact_matching_design_markers(tmp_path: Path, design: str) -> None:
    write_artifacts(tmp_path, design=design)
    with pytest.raises(BaselineResearchError):
        load_baseline_artifacts(tmp_path)


def test_rejects_url_credentials() -> None:
    payload = valid_payload()
    payload["candidates"][0]["source_url"] = "https://user:secret@example.com/paper"
    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_verification_cache_is_digest_and_candidate_bound(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    verification = verification_for(tmp_path)
    assert (
        write_verification(tmp_path, verification).name
        == "BASELINE_RESEARCH_VERIFICATION.json"
    )
    cached = load_cached_verified_baseline(tmp_path)
    assert isinstance(cached, VerifiedBaseline)
    assert cached.verification.research_sha256 == research_sha256(
        cached.artifacts.raw_research
    )
    assert_verified_files(tmp_path, cached)

    (tmp_path / "BASELINE_RESEARCH.json").write_text(
        json.dumps({**valid_payload(), "limitations": ["changed"]}), encoding="utf-8"
    )
    assert load_cached_verified_baseline(tmp_path) is None
    with pytest.raises(BaselineResearchError):
        assert_verified_files(tmp_path, cached)


@pytest.mark.parametrize(
    "cache_mutation",
    [
        lambda data: data.update(selected_candidate_id="linear-probe"),
        lambda data: data.update(research_sha256="0" * 64),
        lambda data: data.update(route="not-a-route"),
    ],
)
def test_ignores_missing_or_invalid_cache(tmp_path: Path, cache_mutation) -> None:
    write_artifacts(tmp_path)
    assert load_cached_verified_baseline(tmp_path) is None
    verification = verification_for(tmp_path).model_dump(mode="json")
    cache_mutation(verification)
    (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").write_text(
        json.dumps(verification), encoding="utf-8"
    )
    assert load_cached_verified_baseline(tmp_path) is None
