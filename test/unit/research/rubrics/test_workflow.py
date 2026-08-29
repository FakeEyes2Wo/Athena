"""Runtime boundary tests for the compact Rubric workflow."""

from pathlib import Path
from types import SimpleNamespace

import json

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_models import Hypothesis, TaskUnderstanding
from athena.core.research_tree import ResearchTree
from athena.research.rubrics.models import (
    EvaluationPolicy,
    EvaluationRubricDraft,
    HypothesisPriorityBatch,
    HypothesisPriorityReview,
    ResourceEstimate,
)
from athena.research.rubrics.workflow import RubricWorkflow


async def _ignore_output(**_kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_evaluation_workflow_preserves_human_metric(tmp_path: Path) -> None:
    understanding = TaskUnderstanding(
        title="diagnosis",
        dataset="medical table",
        target="diagnosis",
        task_type="classification",
        primary_metric="roc_auc",
        direction="maximize",
        metric_source="human",
        human_primary_metric="roc_auc",
        human_direction="maximize",
        readiness="READY",
    )
    runtime = SimpleNamespace(
        _task_text="predict diagnosis",
        _task_understanding=lambda: understanding,
        _store=LocalArtifactStore(tmp_path / "artifacts"),
        publish_output=_ignore_output,
    )
    workflow = RubricWorkflow(runtime, eda_root=lambda: tmp_path)
    calls = 0

    async def fake_agent(**_kwargs):
        nonlocal calls
        calls += 1
        return EvaluationRubricDraft(
            primary_metric="accuracy",
            direction="maximize",
            confidence=0.9,
            explanation="AI suggestion must not override the human.",
        )

    workflow._run_agent = fake_agent  # type: ignore[method-assign]

    policy, ref = await workflow.run_evaluation()

    assert calls == 1
    assert policy.primary_metric == "roc_auc"
    assert policy.metric_source == "human"
    assert await runtime._store.get_text(ref) == policy.model_dump_json()


@pytest.mark.asyncio
async def test_hypothesis_workflow_calls_llm_once_for_whole_batch(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "rubric-test"\ndependencies = ["scikit-learn"]\n',
        encoding="utf-8",
    )
    policy = EvaluationPolicy(
        primary_metric="roc_auc",
        direction="maximize",
        metric_source="human",
        locked=True,
        confidence=1.0,
        explanation="Human objective.",
    )
    runtime = SimpleNamespace(
        _task_text="predict diagnosis",
        _store=LocalArtifactStore(tmp_path / "artifacts"),
        _supervisor=SimpleNamespace(evaluation_policy=policy, evaluator_ref=None),
        tree=ResearchTree(),
        state=SimpleNamespace(search_limit=2, concurrency=1),
        _root=tmp_path,
        _execution=SimpleNamespace(
            environment_root=tmp_path,
            runtime_summary=lambda _root: "Runtime: Windows / PowerShell",
        ),
        publish_output=_ignore_output,
    )
    workflow = RubricWorkflow(runtime, eda_root=lambda: tmp_path)
    hypotheses = [
        Hypothesis(
            id=identifier,
            statement=f"statement {identifier}",
            intervention=f"intervention {identifier}",
            expected_effect="improve ROC AUC",
        )
        for identifier in ("h1", "h2")
    ]
    calls = 0

    async def fake_agent(**kwargs):
        nonlocal calls
        calls += 1
        assert "scikit-learn" in kwargs["content"]
        assert "PowerShell" in kwargs["content"]
        return HypothesisPriorityBatch(
            reviews=[
                HypothesisPriorityReview(
                    hypothesis_id=item.id,
                    evidence_testability=0.8,
                    scientific_value=0.7,
                    resources=ResourceEstimate(
                        latency_penalty=0.2,
                        compute_penalty=0.2,
                        memory_penalty=0.2,
                        api_cost_penalty=0.2,
                        implementation_penalty=0.2,
                        explanation="Small experiment.",
                    ),
                    validity_risk_control=0.9,
                    confidence=0.8,
                    explanation="Auditable priority review.",
                )
                for item in hypotheses
            ]
        )

    workflow._run_agent = fake_agent  # type: ignore[method-assign]

    scored = await workflow.run_hypothesis_priority(hypotheses)

    assert calls == 1
    assert [item.id for item in scored] == ["h1", "h2"]
    assert all(item.rubric_score is not None for item in scored)
    assert all(item.rubric_ref is not None for item in scored)


@pytest.mark.asyncio
async def test_hypothesis_workflow_requires_loaded_frozen_policy(
    tmp_path: Path,
) -> None:
    runtime = SimpleNamespace(
        _task_text="predict diagnosis",
        _store=LocalArtifactStore(tmp_path / "artifacts"),
        _supervisor=SimpleNamespace(evaluation_policy=None, evaluator_ref=None),
        tree=ResearchTree(),
        state=SimpleNamespace(search_limit=2, concurrency=1),
        publish_output=_ignore_output,
    )
    workflow = RubricWorkflow(runtime, eda_root=lambda: tmp_path)
    hypothesis = Hypothesis(
        id="h1",
        statement="test a bounded model",
        intervention="fit the model",
        expected_effect="improve ROC AUC",
    )

    with pytest.raises(RuntimeError, match="frozen Evaluation Policy"):
        await workflow.run_hypothesis_priority([hypothesis])


@pytest.mark.asyncio
async def test_hypothesis_workflow_records_model_failure_and_keeps_fallback(
    tmp_path: Path,
) -> None:
    policy = EvaluationPolicy(
        primary_metric="roc_auc",
        direction="maximize",
        metric_source="human",
        locked=True,
        confidence=1.0,
        explanation="Human objective.",
    )
    outputs: list[dict[str, object]] = []

    async def publish_output(**kwargs) -> None:
        outputs.append(kwargs)

    runtime = SimpleNamespace(
        _task_text="predict diagnosis",
        _store=LocalArtifactStore(tmp_path / "artifacts"),
        _supervisor=SimpleNamespace(evaluation_policy=policy, evaluator_ref=None),
        tree=ResearchTree(),
        state=SimpleNamespace(search_limit=2, concurrency=1),
        publish_output=publish_output,
    )
    workflow = RubricWorkflow(runtime, eda_root=lambda: tmp_path)
    hypothesis = Hypothesis(
        id="h1",
        statement="test a bounded model",
        intervention="fit the model",
        expected_effect="improve ROC AUC",
    )

    async def unavailable_agent(**_kwargs):
        raise RuntimeError("provider unavailable")

    workflow._run_agent = unavailable_agent  # type: ignore[method-assign]

    result = await workflow.run_hypothesis_priority([hypothesis])

    assert result == [hypothesis]
    assert result[0].rubric_score is None
    assert result[0].rubric_ref is None
    fallback = next(item for item in outputs if "确定性回退" in str(item.get("text")))
    assert "[rubric_status=fallback]" in str(fallback["text"])
    fallback_payload = json.loads(
        await runtime._store.get_text(str(fallback["artifact_ref"]))
    )
    assert fallback_payload == {
        "expected_hypothesis_ids": ["h1"],
        "reason": "provider unavailable",
        "reason_type": "RuntimeError",
        "rubric_status": "fallback",
    }
