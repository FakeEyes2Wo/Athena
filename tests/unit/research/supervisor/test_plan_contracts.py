"""Durable autonomous Plan contract tests."""

import json

import pytest
from pydantic import ValidationError

from athena.core.research_models import Hypothesis
from athena.research.supervisor.plans import (
    PlanBest,
    PlanDecision,
    PlanInput,
    PlanState,
)
from athena.research.supervisor.scheduling import Outcome
from athena.research.supervisor.settlement import (
    status_for_outcome as _status_for_outcome,
)

_TRUSTED_REF = "sha256:" + "a" * 64
_OTHER_TRUSTED_REF = "sha256:" + "c" * 64


def test_status_for_outcome_maps_draw_to_inconclusive() -> None:
    assert _status_for_outcome(Outcome.WIN) == "SUPPORTED"
    assert _status_for_outcome(Outcome.LOSS) == "REFUTED"
    assert _status_for_outcome(Outcome.DRAW) == "INCONCLUSIVE"
    assert _status_for_outcome(None) == "INCONCLUSIVE"


def test_plan_decision_requires_an_explicit_structured_decision() -> None:
    with pytest.raises(ValidationError):
        PlanDecision.model_validate({"reason": "I give up"})


@pytest.mark.parametrize("decision", ["continue", "submit", "abandon"])
def test_plan_decision_accepts_each_supported_action(decision: str) -> None:
    parsed = PlanDecision.model_validate({"decision": decision, "reason": "done"})

    assert parsed.decision == decision
    assert parsed.model_dump() == {"decision": decision, "reason": "done"}


def test_plan_decision_rejects_removed_suggestions() -> None:
    with pytest.raises(ValidationError):
        PlanDecision(
            decision="submit",
            reason="done",
            suggestions=[],
        )


def test_plan_input_is_immutable_after_creation() -> None:
    plan_input = PlanInput(
        evaluator_ref=_TRUSTED_REF,
        tree_ref=_OTHER_TRUSTED_REF,
        human_context="Use robust validation",
    )

    with pytest.raises(ValidationError):
        plan_input.human_context = "Use the latest message"


def test_plan_input_json_round_trip_freezes_metric_comparison() -> None:
    plan_input = PlanInput(
        evaluator_ref=_TRUSTED_REF,
        tree_ref=_OTHER_TRUSTED_REF,
        direction="minimize",
        tolerance=0.01,
    )

    loaded = PlanInput.model_validate_json(plan_input.model_dump_json())

    assert loaded.direction == "minimize"
    assert loaded.tolerance == 0.01


@pytest.mark.parametrize("tolerance", [-0.01, float("inf"), float("nan")])
def test_plan_input_rejects_invalid_metric_tolerance(tolerance: float) -> None:
    with pytest.raises(ValidationError):
        PlanInput(
            evaluator_ref=_TRUSTED_REF,
            tree_ref=_OTHER_TRUSTED_REF,
            tolerance=tolerance,
        )


def test_search_plan_requires_patience() -> None:
    with pytest.raises(ValidationError, match="SEARCH Plan requires patience"):
        PlanState(
            kind="SEARCH",
            context_ref=_TRUSTED_REF,
            turns_used=0,
            turn_limit=12,
        )


@pytest.mark.parametrize("kind", ["PREPARE", "VALIDATE"])
def test_non_search_plan_rejects_search_only_fields(kind: str) -> None:
    with pytest.raises(ValidationError, match="SEARCH-only"):
        PlanState(
            kind=kind,
            context_ref=_TRUSTED_REF,
            turns_used=0,
            turn_limit=12,
            patience=4,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("turns_used", -1),
        ("turn_limit", -1),
        ("patience", -1),
        ("stale_rounds", -1),
    ],
)
def test_plan_state_rejects_negative_counters(field: str, value: int) -> None:
    payload = {
        "kind": "SEARCH",
        "context_ref": _TRUSTED_REF,
        "turns_used": 0,
        "turn_limit": 12,
        "patience": 4,
        field: value,
    }

    with pytest.raises(ValidationError):
        PlanState.model_validate(payload)


def test_plan_state_rejects_untrusted_best_reference() -> None:
    with pytest.raises(ValidationError, match="artifact reference"):
        PlanState(
            kind="SEARCH",
            context_ref=_TRUSTED_REF,
            turns_used=1,
            turn_limit=12,
            patience=4,
            best_ref="artifact:claimed-best",
        )


def test_plan_best_requires_trusted_evidence_and_nonblank_commit() -> None:
    best = PlanBest(metric=0.82, commit="abc123", evidence_ref=_TRUSTED_REF)

    assert best.evidence_ref == _TRUSTED_REF
    with pytest.raises(ValidationError):
        PlanBest(metric=0.82, commit="", evidence_ref=_TRUSTED_REF)
    with pytest.raises(ValidationError, match="artifact reference"):
        PlanBest(metric=0.82, commit="abc123", evidence_ref="artifact:evidence")


@pytest.mark.parametrize("metric", [float("nan"), float("inf"), float("-inf")])
def test_plan_best_rejects_non_finite_metric(metric: float) -> None:
    with pytest.raises(ValidationError):
        PlanBest(metric=metric, commit="abc123", evidence_ref=_TRUSTED_REF)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("turns_used", "1"),
        ("turn_limit", "12"),
        ("patience", "4"),
        ("stale_rounds", "0"),
    ],
)
def test_plan_state_rejects_coerced_numeric_counters(field: str, value: str) -> None:
    payload = {
        "kind": "SEARCH",
        "context_ref": _TRUSTED_REF,
        "turns_used": 1,
        "turn_limit": 12,
        "patience": 4,
        field: value,
    }

    with pytest.raises(ValidationError):
        PlanState.model_validate(payload)


@pytest.mark.parametrize(
    "field", ["active_ancestor_hypotheses", "initial_turn_limit", "initial_patience"]
)
def test_plan_input_discards_known_legacy_fields(field: str) -> None:
    plan_input = PlanInput.model_validate_json(
        json.dumps(
            {
                "evaluator_ref": _TRUSTED_REF,
                "tree_ref": _OTHER_TRUSTED_REF,
                field: [],
            }
        )
    )

    assert field not in plan_input.model_dump()


def test_plan_input_still_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(ValidationError):
        PlanInput(
            evaluator_ref=_TRUSTED_REF,
            tree_ref=_OTHER_TRUSTED_REF,
            unknown="ignored",
        )


def test_plan_best_rejects_coerced_metric() -> None:
    with pytest.raises(ValidationError):
        PlanBest(metric="0.82", commit="abc123", evidence_ref=_TRUSTED_REF)


@pytest.mark.parametrize(
    ("contract", "field"),
    [
        (PlanState, "context_ref"),
        (PlanInput, "evaluator_ref"),
        (PlanInput, "tree_ref"),
    ],
)
def test_plan_contracts_reject_untrusted_content_references(
    contract, field: str
) -> None:
    if contract is PlanState:
        payload = {
            "kind": "SEARCH",
            "context_ref": _TRUSTED_REF,
            "turns_used": 0,
            "turn_limit": 12,
            "patience": 4,
        }
    else:
        payload = {
            "evaluator_ref": _TRUSTED_REF,
            "tree_ref": _OTHER_TRUSTED_REF,
        }
    payload[field] = "artifact:untrusted"

    with pytest.raises(ValidationError, match="artifact reference"):
        contract.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [("patience", None), ("stale_rounds", 0), ("best_ref", None)],
)
def test_non_search_plan_rejects_explicit_search_defaults(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError, match="SEARCH-only"):
        PlanState.model_validate(
            {
                "kind": "PREPARE",
                "context_ref": _TRUSTED_REF,
                "turns_used": 0,
                "turn_limit": 12,
                field: value,
            }
        )


@pytest.mark.parametrize("kind", ["PREPARE", "VALIDATE"])
def test_non_search_plan_model_serialization_omits_search_fields(kind: str) -> None:
    plan = PlanState(
        kind=kind,
        context_ref=_TRUSTED_REF,
        turns_used=0,
        turn_limit=12,
    )

    assert set(plan.model_dump()) == {
        "kind",
        "context_ref",
        "turns_used",
        "turn_limit",
    }
    assert all(
        field not in plan.model_dump_json()
        for field in ("patience", "stale_rounds", "best_ref")
    )


def test_plan_input_owns_deeply_immutable_hypothesis_snapshots() -> None:
    source = Hypothesis(
        statement="Try robust scaling",
        intervention="Replace standard scaling",
        expected_effect="Improve validation score",
    )
    plan_input = PlanInput(
        hypothesis=source,
        evaluator_ref=_TRUSTED_REF,
        tree_ref=_OTHER_TRUSTED_REF,
    )

    source.statement = "Mutated source"

    assert plan_input.hypothesis is not None
    assert plan_input.hypothesis.statement == "Try robust scaling"
    with pytest.raises(ValidationError):
        plan_input.hypothesis.statement = "Mutated snapshot"
    with pytest.raises(AttributeError):
        plan_input.hypothesis.sources.append("new-source")
    with pytest.raises(AttributeError):
        plan_input.hypothesis.evidence_refs.append(_TRUSTED_REF)


def test_plan_input_rejects_coerced_nested_hypothesis_counter() -> None:
    with pytest.raises(ValidationError):
        PlanInput(
            hypothesis={
                "statement": "Try robust scaling",
                "intervention": "Replace standard scaling",
                "expected_effect": "Improve validation score",
                "patience": "4",
            },
            evaluator_ref=_TRUSTED_REF,
            tree_ref=_OTHER_TRUSTED_REF,
        )


def test_plan_input_rejects_unknown_nested_hypothesis_field() -> None:
    with pytest.raises(ValidationError):
        PlanInput(
            hypothesis={
                "statement": "Try robust scaling",
                "intervention": "Replace standard scaling",
                "expected_effect": "Improve validation score",
                "unknown": "ignored",
            },
            evaluator_ref=_TRUSTED_REF,
            tree_ref=_OTHER_TRUSTED_REF,
        )


def test_primary_hypothesis_supersedes_is_immutable() -> None:
    plan_input = PlanInput(
        hypothesis=Hypothesis(
            statement="Try robust scaling",
            intervention="Replace standard scaling",
            expected_effect="Improve validation score",
            supersedes=["hyp_old"],
        ),
        evaluator_ref=_TRUSTED_REF,
        tree_ref=_OTHER_TRUSTED_REF,
    )

    assert plan_input.hypothesis is not None
    with pytest.raises(AttributeError):
        plan_input.hypothesis.supersedes.append("hyp_new")


def test_source_hypothesis_supersedes_mutation_does_not_change_snapshot() -> None:
    source = Hypothesis(
        statement="Try robust scaling",
        intervention="Replace standard scaling",
        expected_effect="Improve validation score",
        supersedes=["hyp_old"],
    )
    plan_input = PlanInput(
        hypothesis=source,
        evaluator_ref=_TRUSTED_REF,
        tree_ref=_OTHER_TRUSTED_REF,
    )

    source.supersedes.append("hyp_new")

    assert plan_input.hypothesis is not None
    assert plan_input.hypothesis.supersedes == ("hyp_old",)
