from math import inf, nan

import pytest
from pydantic import ValidationError

from athena.research.experiment_documents.models import (
    DOCUMENTS_STALE_CODE,
    DOCUMENTS_STALE_MESSAGE,
    LatestManifest,
    ProjectionOutcome,
    StageEvent,
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
def test_stage_event_rejects_unsafe_run_ids(run_id: str) -> None:
    payload = _event_payload()
    payload["run_id"] = run_id
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


@pytest.mark.parametrize("value", [nan, inf, -inf])
@pytest.mark.parametrize("field", ["primary", "reference", "generalization_gap"])
def test_stage_event_rejects_non_finite_metrics(field: str, value: float) -> None:
    payload = _event_payload()
    payload["metric"][field] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


def test_stage_event_rejects_non_finite_secondary_metric() -> None:
    payload = _event_payload()
    payload["metric"]["secondary"] = {"f1": nan}  # type: ignore[index]
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


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
def test_stage_event_rejects_invalid_nested_values(path, value) -> None:
    payload = _event_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


def test_models_forbid_extra_fields_at_every_level() -> None:
    payload = _event_payload()
    payload["metric"]["surprise"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        StageEvent.model_validate(payload)


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
def test_stage_event_rejects_invalid_contract_values(field, value) -> None:
    payload = _event_payload()
    if field == "name":
        event = StageEvent.model_validate(payload)
        with pytest.raises(ValidationError):
            StageRecord.from_event(event, name=value, direction="maximize")
    elif field == "direction":
        event = StageEvent.model_validate(payload)
        with pytest.raises(ValidationError):
            StageRecord.from_event(event, name="accuracy", direction=value)
    else:
        payload[field] = value
        with pytest.raises(ValidationError):
            StageEvent.model_validate(payload)


def test_stage_record_adds_resolved_metric_contract() -> None:
    event = StageEvent.model_validate(_event_payload())
    record = StageRecord.from_event(event, name="accuracy", direction="maximize")
    assert record.schema_version == 1
    assert record.metric.name == "accuracy"
    assert record.metric.direction == "maximize"


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


def test_projection_outcome_never_carries_internal_failure_details() -> None:
    failed = ProjectionOutcome.stale()
    assert failed.model_dump() == {
        "ok": False,
        "projection_id": None,
        "warning_code": DOCUMENTS_STALE_CODE,
        "warning_message": DOCUMENTS_STALE_MESSAGE,
    }
    success = ProjectionOutcome.success("a" * 64)
    assert success.ok is True
    assert success.warning_code is None
    with pytest.raises(ValidationError):
        ProjectionOutcome(
            ok=True,
            projection_id=None,
            warning_code=None,
            warning_message=None,
        )
