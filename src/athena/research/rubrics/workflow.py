"""Thin runtime orchestration for the two Rubric agents."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from athena.agents.rubric_agent import (
    EVALUATION_RUBRIC_AGENT_TYPE,
    HYPOTHESIS_RUBRIC_AGENT_TYPE,
)
from athena.core.contracts import ArtifactRef
from athena.core.research_models import Hypothesis
from athena.research.rubrics.evaluation import (
    MetricCapabilityRegistry,
    generate_evaluation_policy,
)
from athena.research.rubrics.models import (
    EvaluationPolicy,
    EvaluationRubricDraft,
    HypothesisPriorityBatch,
    HypothesisPriorityCandidate,
    HypothesisPriorityContext,
    ResearchEvaluationContext,
)
from athena.research.rubrics.ranking import (
    aggregate_hypothesis_priority,
    build_hypothesis_priority_prompt,
    validate_hypothesis_priority_batch,
)
from athena.research.supervisor.experiment import (
    load_agent_result,
    read_eval_handoff as read_evaluator_handoff,
)
from athena.research.supervisor.plans import wait_run_events

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime

RUBRIC_TURN_TIMEOUT_SECONDS = 900


def _artifact_refs(value: object) -> set[ArtifactRef]:
    """Collect artifact-bearing fields from serialized ranking context."""
    refs: set[ArtifactRef] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_ref") and isinstance(item, str) and item.strip():
                refs.add(item)
            elif key in {"evidence_refs", "artifacts"} and isinstance(item, list):
                refs.update(ref for ref in item if isinstance(ref, str) and ref.strip())
            refs.update(_artifact_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_artifact_refs(item))
    return refs


def _read_eda_context(root: Path, limit: int = 8000) -> str:
    """Read a bounded existing EDA handoff without triggering new analysis."""
    chunks: list[str] = []
    remaining = limit
    for name in ("RESEARCH_HANDOFF.md", "HANDOFF.md", "EDA.md", "report.md"):
        path = root / name
        if remaining <= 0 or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")[:remaining]
        except (OSError, UnicodeError):
            continue
        if content.strip():
            chunks.append(f"## {name}\n{content}")
            remaining -= len(content)
    return "\n\n".join(chunks)


class RubricWorkflow:
    """Own Rubric agent calls while deterministic logic stays in pure modules."""

    def __init__(
        self, runtime: "ResearchRuntime", *, eda_root: Callable[[], Path]
    ) -> None:
        self._runtime = runtime
        self._eda_root = eda_root
        self._round = 0

    def _evaluation_context(self) -> ResearchEvaluationContext:
        rt = self._runtime
        understanding = rt._task_understanding()
        if understanding is None:
            raise RuntimeError("task understanding is unavailable")
        task = rt._task_text.strip() or understanding.title.strip()
        if not task:
            raise RuntimeError("research task is blank")
        return ResearchEvaluationContext(
            research_task=task,
            task_understanding=understanding,
            human_primary_metric=understanding.human_primary_metric,
            human_direction=understanding.human_direction,
            official_primary_metric=understanding.official_primary_metric,
            official_direction=understanding.official_direction,
            protocol_primary_metric=understanding.protocol_primary_metric,
            protocol_direction=understanding.protocol_direction,
            dataset_context={
                "dataset": understanding.dataset,
                "target": understanding.target,
                "task_type": understanding.task_type,
            },
            evaluation_feasibility=(
                [understanding.evaluation_plan]
                if understanding.evaluation_plan.strip()
                else []
            ),
            supported_metrics=MetricCapabilityRegistry().as_context(),
        )

    async def _run_agent(
        self,
        *,
        agent_type: str,
        content: str,
        output_type: type[BaseModel],
        label: str,
        agent_id: str | None = None,
    ) -> BaseModel:
        rt = self._runtime
        request = {"content": content, "context_refs": []}
        if agent_id is not None and rt._agents.has_agent(agent_id):
            run_id = await rt._agents.followup(agent_id, request)
        else:
            agent_id, run_id = await rt._agents.create_root(
                agent_type, request, agent_id=agent_id, name=label
            )
        try:
            summary = await asyncio.wait_for(
                wait_run_events(
                    rt._agents,
                    run_id,
                    lambda kind, ref, data: rt._events_bus.project_agent_event(
                        label, kind, ref, data
                    ),
                ),
                timeout=RUBRIC_TURN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as error:
            try:
                await rt._agents.interrupt(agent_id, "rubric_turn_timeout")
            except Exception:
                pass
            raise RuntimeError(f"{label} timed out") from error
        result = await load_agent_result(summary, rt._store, output_type)
        if result is None:
            raise RuntimeError(summary.error or f"{label} failed")
        return result

    async def run_evaluation(self) -> tuple[EvaluationPolicy, ArtifactRef]:
        """Generate and freeze Layer 1 before PREPARE creates the evaluator."""
        rt = self._runtime
        context = self._evaluation_context()
        serialized = json.dumps(
            context.model_dump(mode="json"), ensure_ascii=False, indent=2
        )

        async def _provide(
            _context: ResearchEvaluationContext, correction: str | None
        ) -> EvaluationRubricDraft:
            content = "Create the evaluation policy from this context:\n" + serialized
            if correction:
                content += "\n\nCorrect the invalid recommendation: " + correction
            result = await self._run_agent(
                agent_type=EVALUATION_RUBRIC_AGENT_TYPE,
                content=content,
                output_type=EvaluationRubricDraft,
                label=EVALUATION_RUBRIC_AGENT_TYPE,
                agent_id=EVALUATION_RUBRIC_AGENT_TYPE,
            )
            return EvaluationRubricDraft.model_validate(result)

        policy = await generate_evaluation_policy(context, _provide)
        ref = await rt._store.put_text(policy.model_dump_json())
        await rt.publish_output(
            source="agent",
            channel="text",
            text=(
                "评价策略已冻结："
                f"{policy.primary_metric} / {policy.direction} "
                f"(source={policy.metric_source})。"
            ),
            plan=EVALUATION_RUBRIC_AGENT_TYPE,
            artifact_ref=ref,
        )
        return policy, ref

    async def _priority_context(
        self, hypotheses: list[Hypothesis]
    ) -> HypothesisPriorityContext | None:
        rt = self._runtime
        policy = rt._supervisor.evaluation_policy
        if policy is None:
            return None
        candidates = [
            HypothesisPriorityCandidate(
                hypothesis_id=item.id or "",
                statement=item.statement,
                intervention=item.intervention,
                expected_effect=item.expected_effect,
                cost=item.cost,
                sources=item.sources,
                evidence_refs=item.evidence_refs,
                gate_context={"status": "passed_existing_idea_generation_gate"},
            )
            for item in hypotheses
        ]
        sota_id = rt.tree.best_experiment_id()
        current_sota = (
            rt.tree.get_experiment(sota_id).model_dump(mode="json")
            if sota_id is not None
            else {}
        )
        return HypothesisPriorityContext(
            research_task=rt._task_text,
            evaluation_policy=policy,
            hypotheses=candidates,
            eda_context=_read_eda_context(self._eda_root()),
            evaluator_handoff=await read_evaluator_handoff(
                rt._store, rt._supervisor.evaluator_ref
            ),
            current_sota=current_sota,
            research_history=[
                item.model_dump(mode="json") for item in rt.tree.hypotheses()[-12:]
            ],
            environment_context={
                "search_limit": rt.state.search_limit,
                "concurrency": rt.state.concurrency,
            },
        )

    async def run_hypothesis_priority(
        self, hypotheses: list[Hypothesis]
    ) -> list[Hypothesis]:
        """Score one post-Gate batch once; preserve fallback on any failure."""
        rt = self._runtime
        context = await self._priority_context(hypotheses)
        if context is None:
            return hypotheses
        expected_ids = [item.hypothesis_id for item in context.hypotheses]
        self._round += 1
        label = f"hypothesis-rubric-{self._round}"
        try:
            result = await self._run_agent(
                agent_type=HYPOTHESIS_RUBRIC_AGENT_TYPE,
                content=build_hypothesis_priority_prompt(context),
                output_type=HypothesisPriorityBatch,
                label=label,
            )
            reviews = validate_hypothesis_priority_batch(
                HypothesisPriorityBatch.model_validate(result),
                expected_ids,
                allowed_evidence_refs=_artifact_refs(context.model_dump(mode="json")),
            )
            scored: list[Hypothesis] = []
            for hypothesis in hypotheses:
                if hypothesis.id is None:
                    raise ValueError("ranking candidate is missing an ID")
                review = reviews[hypothesis.id]
                ref = await rt._store.put_text(review.model_dump_json())
                scored.append(
                    hypothesis.model_copy(
                        update={
                            "rubric_score": aggregate_hypothesis_priority(review),
                            "rubric_ref": ref,
                        }
                    )
                )
            await rt.publish_output(
                source="agent",
                channel="text",
                text=f"假设优先级 Rubric 已批量评分 {len(scored)} 个候选。",
                plan=label,
            )
            return scored
        except Exception as error:
            await rt.publish_output(
                source="supervisor",
                channel="error",
                text=f"假设 Rubric 不可用，Selector 使用确定性回退：{error}",
                plan=label,
            )
            return hypotheses


__all__ = ["RubricWorkflow", "read_evaluator_handoff"]
