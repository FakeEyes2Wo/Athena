"""Research Evaluation Rubric precedence, validation, and failure behavior."""

import pytest
from pydantic import ValidationError

from athena.agents.supervisor_agent import _TaskUnderstanding
from athena.core.artifact_store import LocalArtifactStore
from athena.research.contracts import DataScriptBundle
from athena.research.rubrics.evaluation import (
    MetricCapabilityRegistry,
    RubricGenerationError,
    assemble_evaluation_policy,
    generate_evaluation_policy,
)
from athena.research.rubrics.models import (
    EvaluationPolicy,
    EvaluationRubricDraft,
    ResearchEvaluationContext,
)
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.policy import Outcome
from athena.research.supervisor.state import ResearchState
from athena.research.supervisor.supervisor import _compare_metric
from athena.research.supervisor.prepare import _freeze_evaluator


def _context(**updates) -> ResearchEvaluationContext:
    payload = {
        "research_task": "Classify customer messages into service intents",
        "supported_metrics": MetricCapabilityRegistry().as_context(),
    }
    payload.update(updates)
    return ResearchEvaluationContext(**payload)


def _draft(**updates) -> EvaluationRubricDraft:
    payload = {
        "primary_metric": "macro_f1",
        "direction": "maximize",
        "secondary_metrics": ["accuracy", "macro_recall"],
        "guardrails": ["Inspect per-intent recall for rare labels"],
        "confidence": 0.8,
        "explanation": "Macro averaging reflects performance across all intents.",
    }
    payload.update(updates)
    return EvaluationRubricDraft(**payload)


def test_human_explicit_primary_is_locked_and_llm_cannot_override() -> None:
    policy = assemble_evaluation_policy(
        _context(human_primary_metric="Macro-F1"),
        _draft(primary_metric="accuracy", direction="maximize"),
    )

    assert policy.primary_metric == "macro_f1"
    assert policy.metric_source == "human"
    assert policy.locked is True
    assert policy.secondary_metrics == ["accuracy", "macro_recall"]


def test_task_understanding_leaves_unknown_metric_unresolved() -> None:
    understanding = _TaskUnderstanding()

    assert understanding.primary_metric is None
    assert understanding.direction is None
    assert understanding.metric_source == "unresolved"

    with pytest.raises(ValidationError, match="cannot guess"):
        _TaskUnderstanding(primary_metric="accuracy")


def test_task_understanding_can_preserve_multiple_metric_sources() -> None:
    understanding = _TaskUnderstanding(
        primary_metric="macro_f1",
        direction="maximize",
        metric_source="human",
        human_primary_metric="macro_f1",
        official_primary_metric="accuracy",
        official_direction="maximize",
    )

    assert understanding.human_primary_metric == "macro_f1"
    assert understanding.official_primary_metric == "accuracy"


def test_official_metric_precedes_protocol_and_ai() -> None:
    policy = assemble_evaluation_policy(
        _context(
            official_primary_metric="RMSE",
            protocol_primary_metric="mae",
        ),
        _draft(primary_metric="r2", direction="maximize"),
    )

    assert policy.primary_metric == "rmse"
    assert policy.direction == "minimize"
    assert policy.metric_source == "official"


@pytest.mark.asyncio
async def test_unknown_metric_enters_ai_generation_without_accuracy_fallback() -> None:
    calls = []

    async def provider(context, correction):
        calls.append((context, correction))
        return _draft(primary_metric="average_precision", direction="maximize")

    policy = await generate_evaluation_policy(_context(), provider)

    assert len(calls) == 1
    assert policy.primary_metric == "average_precision"
    assert policy.primary_metric != "accuracy"
    assert policy.metric_source == "ai"
    assert policy.locked is False


@pytest.mark.asyncio
async def test_unsupported_metric_is_retried_not_silently_replaced() -> None:
    attempts = 0

    async def provider(_context, correction):
        nonlocal attempts
        attempts += 1
        if correction is None:
            return _draft(primary_metric="invented_score", direction="maximize")
        assert "unsupported primary metric" in correction
        return _draft(primary_metric="macro_f1", direction="maximize")

    policy = await generate_evaluation_policy(_context(), provider)

    assert attempts == 2
    assert policy.primary_metric == "macro_f1"


@pytest.mark.asyncio
async def test_explicit_metric_survives_llm_enrichment_failure() -> None:
    async def broken(_context, _correction):
        raise RuntimeError("provider offline")

    policy = await generate_evaluation_policy(
        _context(human_primary_metric="RMSE"), broken
    )

    assert policy.primary_metric == "rmse"
    assert policy.direction == "minimize"
    assert policy.metric_source == "human"
    assert policy.confidence == 0.0
    assert "without inventing AI advice" in policy.explanation


@pytest.mark.asyncio
async def test_no_primary_and_llm_failure_fails_safely() -> None:
    async def broken(_context, _correction):
        raise RuntimeError("provider offline")

    with pytest.raises(RubricGenerationError, match="could not resolve"):
        await generate_evaluation_policy(_context(), broken)


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"direction": "higher_is_better"}, "direction"),
        ({"primary_metric": ""}, "primary_metric"),
    ],
)
def test_invalid_ai_policy_fields_are_rejected(updates, match) -> None:
    with pytest.raises(ValidationError, match=match):
        _draft(**updates)


def test_ai_direction_must_match_metric_capability() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        assemble_evaluation_policy(
            _context(), _draft(primary_metric="rmse", direction="maximize")
        )


def test_evaluation_rubric_rejects_unknown_evidence_refs() -> None:
    with pytest.raises(ValueError, match="unknown evidence refs"):
        assemble_evaluation_policy(
            _context(evidence_refs=["artifact:known"]),
            _draft(evidence_refs=["artifact:invented"]),
        )


def test_minimize_comparison_treats_lower_rmse_as_better() -> None:
    assert _compare_metric(0.40, 0.50, "minimize", 0.0) is Outcome.WIN
    assert _compare_metric(0.50, 0.40, "minimize", 0.0) is Outcome.LOSS


def test_secondary_guardrails_and_weights_do_not_change_primary_mode() -> None:
    policy = assemble_evaluation_policy(
        _context(),
        _draft(weights={"macro_f1": 0.7, "accuracy": 0.3}),
    )

    assert policy.selection_mode == "primary"
    assert policy.weights == {"macro_f1": 0.7, "accuracy": 0.3}
    assert policy.primary_metric == "macro_f1"


def test_evaluation_policy_ref_survives_resume(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = ResearchState(
        status="RUNNING",
        phase="PREPARE",
        search_limit=2,
        concurrency=1,
        evaluation_policy_ref="sha256:" + "a" * 64,
    )

    state.save(path)
    restored = ResearchState.load(path)

    assert restored.evaluation_policy_ref == state.evaluation_policy_ref
    assert "evaluation_policy_ref" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_evaluator_bundle_must_declare_frozen_policy(tmp_path) -> None:
    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    (evaluator / "evaluate.py").write_text("print('{}')\n", encoding="utf-8")
    (evaluator / "labels.csv").write_text("label\n1\n", encoding="utf-8")
    (evaluator / "metric.json").write_text(
        '{"eval_script":"evaluate.py","primary_metric":"accuracy",'
        '"direction":"maximize"}',
        encoding="utf-8",
    )
    policy = assemble_evaluation_policy(_context(), _draft())

    class Scripts:
        async def freeze(self, _root, metadata):
            return DataScriptBundle(bundle_id="bundle", entrypoint=metadata.entrypoint)

    with pytest.raises(ValueError, match="does not match frozen"):
        await _freeze_evaluator(
            root=evaluator,
            scripts=Scripts(),
            store=LocalArtifactStore(tmp_path / "artifacts"),
            evaluation_policy=policy,
        )


@pytest.mark.asyncio
async def test_runtime_applies_policy_before_downstream_comparisons(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path, direction="maximize")
    runtime.state.task_understanding = {
        "title": "regression",
        "primary_metric": "rmse",
        "direction": "minimize",
        "metric_source": "human",
    }
    policy = EvaluationPolicy(
        primary_metric="rmse",
        direction="minimize",
        metric_source="human",
        locked=True,
        confidence=0.9,
        explanation="The Human explicitly selected RMSE.",
    )
    ref = await runtime._store.put_text(policy.model_dump_json())

    async def fake_generation():
        return policy, ref

    runtime._agent_turns.run_evaluation_rubric = fake_generation  # type: ignore[method-assign]

    await runtime._ensure_evaluation_policy()

    assert runtime.supervisor.evaluation_policy == policy
    assert runtime.supervisor._direction == "minimize"
    assert runtime.state.evaluation_policy_ref == ref
    await runtime.aclose()
