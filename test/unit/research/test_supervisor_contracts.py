"""Tests for retained research input and EDA result contracts."""

import pytest
from pydantic import ValidationError

from athena.research.contracts import (
    EDAAttemptOutcome,
    EDARepairFailure,
    ExecutionConfig,
)

_ART = "artifact://sha256:" + "0" * 64


def test_execution_config_defaults_and_limits() -> None:
    config = ExecutionConfig()
    assert config.k_folds == 5
    assert (config.ideator_count, config.hypotheses_per_ideator) == (3, 2)
    with pytest.raises(ValidationError):
        ExecutionConfig(ideator_count=9)
    with pytest.raises(ValidationError):
        ExecutionConfig(hypotheses_per_ideator=0)


def test_eda_attempt_outcome_contract_success_and_failure() -> None:
    success = EDAAttemptOutcome(
        status="succeeded",
        execution_id="exec_1",
        workspace="/tmp/ws",
        bundle_ref=_ART,
    )
    assert success.bundle_ref == _ART
    assert success.failure_ref is None

    failure = EDARepairFailure(
        attempt=1,
        command=["python", "analysis.py", "data.csv", "label"],
        exit_code=1,
        stderr="AttributeError: labels",
        failure_signature="sig-v1",
    )
    repair = EDAAttemptOutcome(
        status="repairable_failure",
        execution_id="exec_1",
        workspace="/tmp/ws",
        repair_count=1,
        failure_ref=_ART,
        failure_signature=failure.failure_signature,
    )
    assert repair.bundle_ref is None
    assert repair.failure_ref == _ART
    with pytest.raises(ValidationError):
        EDAAttemptOutcome(status="bogus", execution_id="e", workspace="w")


def test_eda_attempt_outcome_roundtrips_json() -> None:
    outcome = EDAAttemptOutcome(
        status="repairable_failure",
        execution_id="exec_1",
        workspace="/tmp/ws",
        repair_count=2,
        failure_ref=_ART,
        failure_signature="sig-v2",
    )
    assert EDAAttemptOutcome.model_validate_json(outcome.model_dump_json()) == outcome
