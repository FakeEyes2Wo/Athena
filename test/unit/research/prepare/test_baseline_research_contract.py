import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from athena.research.prepare import baseline_research as baseline_contract
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
        "schema_version": 2,
        "dataset": {
            "modality": "image",
            "task_type": "classification",
            "input_scale": "224x224 RGB",
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
                }
            ],
            "rationale": "Few labels relative to image dimensionality.",
        },
        "training": {
            "strategy": "partial_finetune",
            "pretrained": {
                "status": "available",
                "representation": "ImageNet encoder",
                "evidence": {
                    "kind": "source",
                    "reference": "https://example.org/paper",
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
        "search": {
            "queries": ["query one", "query two"],
            "one_candidate": None,
        },
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
        "schema_version": 2,
        "research_sha256": research_sha256(artifacts.raw_research),
        "design_sha256": baseline_contract.design_sha256(artifacts.raw_design),
        "selected_candidate_id": artifacts.selected.candidate_id,
        "route": "git",
        "verified_at": datetime.now(timezone.utc),
        "repository_url": str(artifacts.selected.repository_url),
        "commit": "a" * 40,
        "attempts": [{"route": "git", "success": True, "diagnostic": "clone verified"}],
    }
    values.update(changes)
    return BaselineVerification.model_validate(values)


def openalex_verification_for(root: Path, **changes: object) -> BaselineVerification:
    artifacts = load_baseline_artifacts(root)
    base = verification_for(root)
    values: dict[str, object] = {
        "route": "openalex",
        "repository_url": None,
        "commit": None,
        "paper_locator": artifacts.selected.paper_locator,
        "openalex_id": "W123",
        "title": artifacts.selected.title,
        "publication_year": 2016,
        "cited_by_count": 100,
        "attempts": (
            base.attempts[0].model_copy(
                update={
                    "route": "openalex",
                    "success": True,
                    "diagnostic": "authority threshold verified",
                }
            ),
        ),
    }
    values.update(changes)
    return base.model_copy(update=values)


def test_loads_matching_research_and_design(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    assert artifacts.selected.candidate_id == "resnet-transfer"
    assert artifacts.design.training_strategy == "partial_finetune"
    assert artifacts.research.training.strategy == "partial_finetune"


def test_load_retains_exact_design_bytes(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    raw_design = (tmp_path / "BASELINE_DESIGN.md").read_bytes()

    artifacts = load_baseline_artifacts(tmp_path)

    assert artifacts.raw_design == raw_design


def test_design_digest_distinguishes_lf_from_crlf() -> None:
    lf = b"Selected candidate: `resnet-transfer`\n"
    crlf = b"Selected candidate: `resnet-transfer`\r\n"

    assert baseline_contract.design_sha256(lf) != baseline_contract.design_sha256(crlf)


def test_rejects_design_that_is_not_strict_utf8(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    (tmp_path / "BASELINE_DESIGN.md").write_bytes(b"\xff\xfe")

    with pytest.raises(BaselineResearchError, match="unable to read BASELINE_DESIGN"):
        load_baseline_artifacts(tmp_path)


@pytest.mark.parametrize(
    "field",
    [
        "labeled_samples",
        "effective_training_units",
        "group_count",
        "class_count",
        "minority_class_samples",
    ],
)
@pytest.mark.parametrize(
    "defect",
    [
        "missing_value",
        "missing_evidence",
        "blank_reference",
        "blank_claim",
        "negative_value",
        "source_evidence",
    ],
)
def test_each_numeric_fact_requires_bound_local_evidence(
    field: str, defect: str
) -> None:
    payload = valid_payload()
    fact = payload["dataset"]["facts"][0]
    fact["field"] = field
    fact["value"] = 5
    fact["evidence"]["claim"] = "The measured value is 5."
    if defect == "missing_value":
        del fact["value"]
    elif defect == "missing_evidence":
        del fact["evidence"]
    elif defect == "blank_reference":
        fact["evidence"]["reference"] = "   "
    elif defect == "blank_claim":
        fact["evidence"]["claim"] = "   "
    elif defect == "negative_value":
        fact["value"] = -5
        fact["evidence"]["claim"] = "The measured value is -5."
    else:
        fact["evidence"]["kind"] = "source"

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "labeled_samples",
        "effective_training_units",
        "group_count",
        "class_count",
        "minority_class_samples",
    ],
)
def test_rejects_duplicate_numeric_fact_names(field: str) -> None:
    payload = valid_payload()
    fact = payload["dataset"]["facts"][0]
    fact["field"] = field
    payload["dataset"]["facts"].append(dict(fact))

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


@pytest.mark.parametrize("kind", ["eda", "data_contract"])
def test_non_calculation_fact_is_structurally_bound_without_repeating_value(
    kind: str,
) -> None:
    payload = valid_payload()
    payload["dataset"]["facts"][0]["evidence"].update(
        kind=kind,
        reference="EDA_HANDOFF.md#training-split",
        claim="Counted after applying the documented leakage-safe split.",
    )

    assert (
        BaselineResearch.model_validate(payload).dataset.facts[0].evidence.kind == kind
    )


@pytest.mark.parametrize(
    "claim",
    [
        "There are 480 labeled images.",
        "480 + 0 = 481 labeled images.",
        "400 / 0 = 480 labeled images.",
        "400 / 3 = 480 labeled images.",
        "-240 + 240 = 480 labeled images.",
        "- 240 + 240 = 480",
        "240 + 240 = 480,000 labeled images.",
        "٢٤٠ + ٢٤٠ = ٤٨٠",
        "Derived as 240 + 240 = 480",
    ],
)
def test_calculation_evidence_requires_a_true_integer_expression(claim: str) -> None:
    payload = valid_payload()
    payload["dataset"]["facts"][0]["evidence"].update(kind="calculation", claim=claim)

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


@pytest.mark.parametrize("location", ["reference", "claim"])
@pytest.mark.parametrize(
    "expression",
    ["240 + 240 = 480", "500 - 20 = 480", "24 * 20 = 480", "960 / 2 = 480"],
)
def test_calculation_expression_may_be_in_reference_or_claim(
    location: str, expression: str
) -> None:
    payload = valid_payload()
    evidence = payload["dataset"]["facts"][0]["evidence"]
    evidence.update(
        kind="calculation",
        reference="EDA_HANDOFF.md#dataset-size",
        claim="Derived from two leakage-safe folds.",
    )
    evidence[location] = f"  {expression}  "

    assert BaselineResearch.model_validate(payload).dataset.facts[0].value == 480


def scratch_scale() -> dict:
    return {
        "selected_candidate_id": "resnet-transfer",
        "source_locator": "https://arxiv.org/abs/1512.03385",
        "local_fact": "labeled_samples",
        "local_value": 480,
        "source_value": 400,
        "unit": "labeled images",
        "relationship": "comparable",
        "rationale": "The local and cited experiments use comparable labeled scale.",
    }


def safeguards() -> dict:
    return {
        "augmentation": {
            "kind": "source",
            "reference": "https://example.org/augmentation",
            "claim": "Random crops mitigate overfitting.",
        },
        "regularization": {
            "kind": "source",
            "reference": "https://example.org/regularization",
            "claim": "Weight decay is used during fine-tuning.",
        },
        "validation": {
            "kind": "eda",
            "reference": "EDA_HANDOFF.md#split",
            "claim": "The grouped validation split prevents leakage.",
        },
    }


@pytest.mark.parametrize(
    ("modality", "regime", "strategy", "pretrained_status", "with_safeguards"),
    [
        ("image", "tiny", "frozen_pretrained", "available", False),
        ("text", "small", "partial_finetune", "available", False),
        ("audio", "small", "full_finetune", "available", True),
        ("video", "adequate", "full_finetune", "available", True),
        ("multimodal", "unknown", "frozen_pretrained", "available", False),
        ("image", "unknown", "classical", "unavailable", False),
        ("tabular", "small", "classical", None, False),
        ("tabular", "small", "partial_finetune", "available", False),
    ],
)
def test_accepts_deterministic_training_policy_matrix(
    modality: str,
    regime: str,
    strategy: str,
    pretrained_status: str | None,
    with_safeguards: bool,
) -> None:
    payload = valid_payload()
    payload["dataset"].update(modality=modality, regime=regime)
    payload["training"]["strategy"] = strategy
    if pretrained_status is None:
        payload["training"]["pretrained"] = None
    else:
        payload["training"]["pretrained"]["status"] = pretrained_status
        if pretrained_status != "available":
            payload["training"]["pretrained"]["representation"] = None
    if with_safeguards:
        payload["training"]["safeguards"] = safeguards()

    assert BaselineResearch.model_validate(payload).training.strategy == strategy


@pytest.mark.parametrize(
    ("regime", "strategy", "pretrained_status", "with_safeguards"),
    [
        ("tiny", "partial_finetune", "available", False),
        ("tiny", "full_finetune", "available", True),
        ("small", "train_from_scratch", "available", False),
        ("small", "full_finetune", "available", False),
        ("unknown", "partial_finetune", "available", False),
        ("unknown", "full_finetune", "available", True),
        ("unknown", "train_from_scratch", "available", False),
        ("small", "frozen_pretrained", "unavailable", False),
        ("adequate", "partial_finetune", "unknown", False),
    ],
)
def test_rejects_deterministic_training_policy_matrix(
    regime: str,
    strategy: str,
    pretrained_status: str,
    with_safeguards: bool,
) -> None:
    payload = valid_payload()
    payload["dataset"]["regime"] = regime
    payload["training"]["strategy"] = strategy
    payload["training"]["pretrained"]["status"] = pretrained_status
    if pretrained_status != "available":
        payload["training"]["pretrained"]["representation"] = None
    if with_safeguards:
        payload["training"]["safeguards"] = safeguards()

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_full_finetune_requires_all_three_safeguards() -> None:
    payload = valid_payload()
    payload["training"]["strategy"] = "full_finetune"
    payload["training"]["safeguards"] = safeguards()
    del payload["training"]["safeguards"]["regularization"]

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


@pytest.mark.parametrize("field", ["pretrained", "safeguards", "scratch_scale"])
def test_rejects_missing_or_unused_training_evidence(field: str) -> None:
    payload = valid_payload()
    if field == "pretrained":
        payload["training"]["pretrained"] = None
    elif field == "safeguards":
        payload["training"]["safeguards"] = safeguards()
    else:
        payload["training"]["scratch_scale"] = scratch_scale()

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_accepts_scratch_only_with_selected_source_scale_comparison() -> None:
    payload = valid_payload()
    payload["dataset"]["regime"] = "adequate"
    payload["training"]["strategy"] = "train_from_scratch"
    payload["training"]["scratch_scale"] = scratch_scale()

    research = BaselineResearch.model_validate(payload)

    assert research.training.scratch_scale.local_value == 480


@pytest.mark.parametrize(
    ("selected_url", "comparison_locator"),
    [
        ("https://example.org", "https://example.org"),
        ("https://example.org/model", "https://EXAMPLE.ORG/model"),
    ],
)
def test_scratch_source_accepts_equivalent_http_locator(
    selected_url: str, comparison_locator: str
) -> None:
    payload = valid_payload()
    payload["dataset"]["regime"] = "adequate"
    payload["training"]["strategy"] = "train_from_scratch"
    payload["training"]["scratch_scale"] = scratch_scale()
    payload["candidates"][0]["source_url"] = selected_url
    payload["training"]["scratch_scale"]["source_locator"] = comparison_locator

    research = BaselineResearch.model_validate(payload)

    assert research.training.scratch_scale.source_locator == comparison_locator


def test_scratch_source_rejects_a_different_http_locator() -> None:
    payload = valid_payload()
    payload["dataset"]["regime"] = "adequate"
    payload["training"]["strategy"] = "train_from_scratch"
    payload["training"]["scratch_scale"] = scratch_scale()
    payload["candidates"][0]["source_url"] = "https://example.org/model"
    payload["training"]["scratch_scale"][
        "source_locator"
    ] = "https://other.example.org/model"

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_rejects_adequate_scratch_without_scale_comparison() -> None:
    payload = valid_payload()
    payload["dataset"]["regime"] = "adequate"
    payload["training"]["strategy"] = "train_from_scratch"

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("selected_candidate_id", "linear-probe"),
        ("source_locator", "https://example.org/unselected-source"),
        ("local_fact", "group_count"),
        ("local_value", 481),
    ],
)
def test_rejects_scratch_scale_mismatch(field: str, value: object) -> None:
    payload = valid_payload()
    payload["dataset"]["regime"] = "adequate"
    payload["training"]["strategy"] = "train_from_scratch"
    payload["training"]["scratch_scale"] = scratch_scale()
    payload["training"]["scratch_scale"][field] = value

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


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

    payload["search"]["one_candidate"] = {
        "query_indices": [0, 1],
        "scope": "Official repositories and primary papers.",
        "limitation": "Only one relevant implementation was found.",
    }
    assert len(BaselineResearch.model_validate(payload).candidates) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope", "   "),
        ("limitation", "   "),
        ("query_indices", [0, 0]),
        ("query_indices", [0, 2]),
    ],
)
def test_rejects_invalid_one_candidate_exception(field: str, value: object) -> None:
    payload = valid_payload()
    payload["candidates"] = payload["candidates"][:1]
    payload["decisions"] = payload["decisions"][:1]
    payload["search"]["one_candidate"] = {
        "query_indices": [0, 1],
        "scope": "Official repositories and primary papers.",
        "limitation": "Only one relevant implementation was found.",
    }
    payload["search"]["one_candidate"][field] = value

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_rejects_redundant_one_candidate_exception() -> None:
    payload = valid_payload()
    payload["search"]["one_candidate"] = {
        "query_indices": [0, 1],
        "scope": "Official repositories and primary papers.",
        "limitation": "Only one relevant implementation was found.",
    }

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


def test_rejects_blank_or_duplicate_search_queries() -> None:
    payload = valid_payload()
    payload["search"]["queries"] = ["Query", " query "]

    with pytest.raises(ValidationError):
        BaselineResearch.model_validate(payload)


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


def test_same_markers_with_different_design_body_do_not_match_verification(
    tmp_path: Path,
) -> None:
    original = (
        "# Baseline\n\nSelected candidate: `resnet-transfer`\n"
        "Training strategy: `partial_finetune`\n\nOriginal adaptation.\n"
    )
    write_artifacts(tmp_path, design=original)
    verification = verification_for(tmp_path)

    changed = original.replace("Original adaptation.", "Different adaptation.")
    write_artifacts(tmp_path, design=changed)
    artifacts = load_baseline_artifacts(tmp_path)

    with pytest.raises(BaselineResearchError, match="design artifact digest"):
        baseline_contract.assert_verification_matches_artifacts(artifacts, verification)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "[::1]"])
def test_rejects_non_public_repository_in_cached_proof(
    tmp_path: Path, host: str
) -> None:
    write_artifacts(tmp_path)

    with pytest.raises(ValidationError):
        verification_for(tmp_path, repository_url=f"https://{host}/repo.git")


def test_rejects_safe_repository_proof_for_a_non_selected_candidate(
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    verification = verification_for(
        tmp_path, repository_url="https://github.com/pytorch/tutorials.git"
    )

    with pytest.raises(BaselineResearchError, match="selected repository"):
        baseline_contract.assert_verification_matches_artifacts(artifacts, verification)


def test_rejects_arbitrary_high_citation_openalex_proof(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    verification = openalex_verification_for(
        tmp_path,
        paper_locator="doi:10.0000/unrelated",
        openalex_id="W999999",
        title="An unrelated highly cited work",
        cited_by_count=1_000_000,
    )

    with pytest.raises(BaselineResearchError):
        baseline_contract.assert_verification_matches_artifacts(artifacts, verification)


def test_rejects_openalex_proof_for_a_different_paper_locator(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    verification = openalex_verification_for(
        tmp_path, paper_locator="doi:10.0000/other"
    )

    with pytest.raises(BaselineResearchError, match="paper locator"):
        baseline_contract.assert_verification_matches_artifacts(artifacts, verification)


def test_rejects_openalex_proof_with_a_mismatched_title(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    verification = openalex_verification_for(
        tmp_path, title="An unrelated paper about another subject entirely"
    )

    with pytest.raises(BaselineResearchError, match="title"):
        baseline_contract.assert_verification_matches_artifacts(artifacts, verification)


@pytest.mark.parametrize(
    "openalex_id", ["", "W0", "W01", "w123", "https://openalex.org/W123"]
)
def test_rejects_noncanonical_openalex_work_id(
    tmp_path: Path, openalex_id: str
) -> None:
    write_artifacts(tmp_path)
    values = openalex_verification_for(tmp_path).model_dump()
    values["openalex_id"] = openalex_id

    with pytest.raises(ValidationError):
        BaselineVerification.model_validate(values)


def test_git_proof_rejects_a_paper_locator(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    verification = verification_for(tmp_path).model_copy(
        update={"paper_locator": "doi:10.1109/CVPR.2016.90"}
    )

    with pytest.raises(ValidationError):
        BaselineVerification.model_validate(verification.model_dump())


def test_verification_serialization_is_canonical_and_newline_terminated(
    tmp_path: Path,
) -> None:
    write_artifacts(tmp_path)
    verification = verification_for(tmp_path)

    assert baseline_contract.verification_bytes(verification) == (
        verification.model_dump_json(indent=2) + "\n"
    ).encode("utf-8")


def test_verification_schema_version_is_explicit(tmp_path: Path) -> None:
    write_artifacts(tmp_path)
    values = verification_for(tmp_path).model_dump()
    del values["schema_version"]

    with pytest.raises(ValidationError):
        BaselineVerification.model_validate(values)


@pytest.mark.parametrize("route", ["git", "openalex"])
def test_rejects_incomplete_matching_digest_cache(tmp_path: Path, route: str) -> None:
    write_artifacts(tmp_path)
    artifacts = load_baseline_artifacts(tmp_path)
    forged = {
        "research_sha256": research_sha256(artifacts.raw_research),
        "selected_candidate_id": artifacts.selected.candidate_id,
        "route": route,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "attempts": [],
    }
    (tmp_path / "BASELINE_RESEARCH_VERIFICATION.json").write_text(
        json.dumps(forged), encoding="utf-8"
    )
    assert load_cached_verified_baseline(tmp_path) is None


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
