from math import inf, nan

import pytest
from pydantic import ValidationError

from athena.research.experiment_documents.models import (
    LatestManifest,
    StageRecord,
)


def _event_payload() -> dict[str, object]:
    return {
        "run_id": "exp_search-1",
        "stage": "search",
        "status": "SUCCEEDED",
        "metric": {
            "primary": 0.81,
            "reference": 0.78,
            "generalization_gap": None,
            "secondary": {"f1": 0.79},
        },
        "artifacts": {"evidence": "sha256:evidence"},
        "reason": {"kind": "trusted_score", "summary": "Trusted score won."},
        "provenance": {
            "experiment_id": "exp_search-1",
            "hypothesis_id": "search-1",
            "commit": "abc123",
        },
    }


@pytest.mark.parametrize(
    "run_id",
    ["", "../escape", "a..b", "a/b", r"a\\b", "a:", "name.", "CON"],
)
def test_stage_record_rejects_unsafe_event_run_ids(run_id: str) -> None:
    payload = _event_payload()
    payload["run_id"] = run_id
    with pytest.raises(ValueError):
        StageRecord.from_event(payload, name="accuracy", direction="maximize")


@pytest.mark.parametrize("value", [nan, inf, -inf])
@pytest.mark.parametrize("field", ["primary", "reference", "generalization_gap"])
def test_stage_record_rejects_non_finite_event_metrics(
    field: str, value: float
) -> None:
    payload = _event_payload()
    payload["metric"][field] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        StageRecord.from_event(payload, name="accuracy", direction="maximize")


def test_stage_record_rejects_non_finite_secondary_metric() -> None:
    payload = _event_payload()
    payload["metric"]["secondary"] = {"f1": nan}  # type: ignore[index]
    with pytest.raises(ValidationError):
        StageRecord.from_event(payload, name="accuracy", direction="maximize")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("stage",), "prepare"),
        (("status",), "succeeded"),
        (("artifacts",), {"": "ref"}),
        (("reason", "summary"), ""),
        (("provenance", "commit"), "\n"),
    ],
)
def test_stage_record_rejects_invalid_event_values(path, value) -> None:
    payload = _event_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        StageRecord.from_event(payload, name="accuracy", direction="maximize")


def test_models_forbid_extra_fields_at_every_level() -> None:
    payload = _event_payload()
    payload["metric"]["surprise"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        StageRecord.from_event(payload, name="accuracy", direction="maximize")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", " accuracy"),
        ("direction", "sideways"),
        ("reason", {"kind": "bad-kind", "summary": "x"}),
        ("provenance", {}),
    ],
)
def test_stage_record_rejects_invalid_contract_values(field, value) -> None:
    payload = _event_payload()
    if field == "name":
        with pytest.raises(ValidationError):
            StageRecord.from_event(payload, name=value, direction="maximize")
    elif field == "direction":
        with pytest.raises(ValidationError):
            StageRecord.from_event(payload, name="accuracy", direction=value)
    else:
        payload[field] = value
        with pytest.raises(ValidationError):
            StageRecord.from_event(payload, name="accuracy", direction="maximize")


def test_stage_record_adds_resolved_metric_contract() -> None:
    record = StageRecord.from_event(
        _event_payload(), name="accuracy", direction="maximize"
    )
    assert record.schema_version == 1
    assert record.metric.name == "accuracy"
    assert record.metric.direction == "maximize"


@pytest.mark.parametrize("field", ["schema_version", "name", "direction"])
def test_stage_record_rejects_projector_owned_event_fields(field: str) -> None:
    payload = _event_payload()
    if field in {"name", "direction"}:
        payload["metric"][field] = "injected"  # type: ignore[index]
    else:
        payload[field] = 1
    with pytest.raises(ValueError):
        StageRecord.from_event(payload, name="accuracy", direction="maximize")


def test_latest_manifest_requires_stage_identity_only_for_stage_kind() -> None:
    digest = "a" * 64
    stage = LatestManifest(
        schema_version=1,
        projection_id=digest,
        kind="stage",
        stage="search",
        run_id="exp_search-1",
        files={"search.json": digest},
    )
    assert stage.run_id == "exp_search-1"
    invalid = stage.model_dump(mode="json")
    invalid["run_id"] = None
    with pytest.raises(ValidationError):
        LatestManifest.model_validate(invalid)


def test_latest_manifest_rejects_invalid_identity_and_digest() -> None:
    digest = "a" * 64
    with pytest.raises(ValidationError):
        LatestManifest(
            schema_version=1,
            projection_id="A" * 64,
            kind="rebuild",
            files={"../latest.json": digest},
        )
