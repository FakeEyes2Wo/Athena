"""Deterministic metric precedence, capability validation, and safe failure."""

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from athena.core.research_models import MetricDirection
from athena.research.rubrics.models import (
    EvaluationPolicy,
    EvaluationRubricDraft,
    MetricPolicySource,
    ResearchEvaluationContext,
)

_METRIC_TOKEN = re.compile(r"[^a-z0-9]+")


def normalize_metric_name(value: str) -> str:
    """Normalize spelling only; never choose a metric."""
    normalized = _METRIC_TOKEN.sub("_", value.strip().lower()).strip("_")
    if not normalized:
        raise ValueError("primary metric must be nonblank")
    return normalized


@dataclass(frozen=True)
class MetricCapability:
    """One metric a generated evaluator is expected to implement."""

    name: str
    direction: MetricDirection
    aliases: tuple[str, ...] = ()


DEFAULT_METRIC_CAPABILITIES: tuple[MetricCapability, ...] = (
    MetricCapability("accuracy", "maximize"),
    MetricCapability("balanced_accuracy", "maximize"),
    MetricCapability("f1", "maximize", ("f1_score",)),
    MetricCapability("macro_f1", "maximize", ("f1_macro", "macro_f1_score")),
    MetricCapability("micro_f1", "maximize", ("f1_micro",)),
    MetricCapability("weighted_f1", "maximize", ("f1_weighted",)),
    MetricCapability("precision", "maximize"),
    MetricCapability("macro_precision", "maximize"),
    MetricCapability("recall", "maximize", ("sensitivity",)),
    MetricCapability("macro_recall", "maximize"),
    MetricCapability("specificity", "maximize"),
    MetricCapability("roc_auc", "maximize", ("auc", "auroc")),
    MetricCapability("average_precision", "maximize", ("pr_auc", "auprc")),
    MetricCapability("log_loss", "minimize", ("cross_entropy",)),
    MetricCapability("mse", "minimize", ("mean_squared_error",)),
    MetricCapability("rmse", "minimize", ("root_mean_squared_error",)),
    MetricCapability("mae", "minimize", ("mean_absolute_error",)),
    MetricCapability("mape", "minimize"),
    MetricCapability("r2", "maximize", ("r_squared",)),
    MetricCapability("ndcg", "maximize"),
    MetricCapability("map", "maximize", ("mean_average_precision",)),
    MetricCapability("mrr", "maximize"),
    MetricCapability("bleu", "maximize"),
    MetricCapability("rouge_l", "maximize"),
    MetricCapability("exact_match", "maximize"),
    MetricCapability("perplexity", "minimize"),
    MetricCapability("panoptic_quality", "maximize"),
)


class MetricCapabilityRegistry:
    """Validation menu; it never performs scientific metric selection."""

    def __init__(
        self, capabilities: Iterable[MetricCapability] = DEFAULT_METRIC_CAPABILITIES
    ) -> None:
        self._canonical: dict[str, MetricCapability] = {}
        self._lookup: dict[str, MetricCapability] = {}
        for capability in capabilities:
            canonical = normalize_metric_name(capability.name)
            if canonical in self._canonical:
                raise ValueError(f"duplicate metric capability: {canonical}")
            normalized = MetricCapability(
                canonical, capability.direction, capability.aliases
            )
            self._canonical[canonical] = normalized
            for name in (canonical, *capability.aliases):
                alias = normalize_metric_name(name)
                if alias in self._lookup:
                    raise ValueError(f"duplicate metric alias: {alias}")
                self._lookup[alias] = normalized

    def resolve(self, name: str) -> MetricCapability:
        """Resolve one explicit metric or fail without substituting accuracy."""
        token = normalize_metric_name(name)
        try:
            return self._lookup[token]
        except KeyError as error:
            raise ValueError(f"unsupported primary metric: {token}") from error

    def as_context(self) -> dict[str, MetricDirection]:
        """Return canonical metric names and directions for the AI context."""
        return {name: item.direction for name, item in self._canonical.items()}


@dataclass(frozen=True)
class _PrimaryChoice:
    metric: str
    direction: MetricDirection | None
    source: MetricPolicySource


def _locked_primary(context: ResearchEvaluationContext) -> _PrimaryChoice | None:
    choices = (
        (context.human_primary_metric, context.human_direction, "human"),
        (context.official_primary_metric, context.official_direction, "official"),
        (context.protocol_primary_metric, context.protocol_direction, "protocol"),
    )
    for metric, direction, source in choices:
        if metric is not None and metric.strip():
            return _PrimaryChoice(metric, direction, source)  # type: ignore[arg-type]
    return None


def _validate_choice(
    choice: _PrimaryChoice, registry: MetricCapabilityRegistry
) -> tuple[str, MetricDirection]:
    capability = registry.resolve(choice.metric)
    if choice.direction is not None and choice.direction != capability.direction:
        raise ValueError(
            f"direction {choice.direction!r} conflicts with supported metric "
            f"{capability.name!r} ({capability.direction})"
        )
    return capability.name, capability.direction


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def assemble_evaluation_policy(
    context: ResearchEvaluationContext,
    draft: EvaluationRubricDraft,
    *,
    registry: MetricCapabilityRegistry | None = None,
) -> EvaluationPolicy:
    """Apply source precedence and validate the selected capability."""
    active_registry = registry or MetricCapabilityRegistry()
    unexpected_refs = set(draft.evidence_refs) - set(context.evidence_refs)
    if unexpected_refs:
        raise ValueError(
            "evaluation rubric contains unknown evidence refs: "
            f"{sorted(unexpected_refs)}"
        )
    locked = _locked_primary(context)
    if locked is None:
        capability = active_registry.resolve(draft.primary_metric)
        if draft.direction != capability.direction:
            raise ValueError(
                f"direction {draft.direction!r} conflicts with supported metric "
                f"{capability.name!r} ({capability.direction})"
            )
        metric, direction, source = capability.name, capability.direction, "ai"
    else:
        metric, direction = _validate_choice(locked, active_registry)
        source = locked.source
    return EvaluationPolicy(
        primary_metric=metric,
        direction=direction,
        metric_source=source,
        locked=locked is not None,
        secondary_metrics=_dedupe(draft.secondary_metrics),
        guardrails=_dedupe(draft.guardrails),
        confidence=draft.confidence,
        explanation=draft.explanation,
        evidence_refs=_dedupe([*context.evidence_refs, *draft.evidence_refs]),
    )


class RubricGenerationError(RuntimeError):
    """No valid primary metric policy could be generated safely."""


DraftProvider = Callable[
    [ResearchEvaluationContext, str | None], Awaitable[EvaluationRubricDraft]
]


async def generate_evaluation_policy(
    context: ResearchEvaluationContext,
    provider: DraftProvider,
    *,
    registry: MetricCapabilityRegistry | None = None,
    max_attempts: int = 2,
) -> EvaluationPolicy:
    """Retry invalid AI output and preserve only an explicit locked primary."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    active_registry = registry or MetricCapabilityRegistry()
    correction: str | None = None
    last_error: BaseException | None = None
    for _ in range(max_attempts):
        try:
            draft = await provider(context, correction)
            return assemble_evaluation_policy(context, draft, registry=active_registry)
        except Exception as error:
            last_error = error
            correction = " ".join(str(error).split())[:1000]
    locked = _locked_primary(context)
    if locked is not None:
        metric, direction = _validate_choice(locked, active_registry)
        return EvaluationPolicy(
            primary_metric=metric,
            direction=direction,
            metric_source=locked.source,
            locked=True,
            confidence=0.0,
            explanation=(
                "AI rubric enrichment was unavailable; Athena preserved the "
                f"locked {locked.source} primary metric without inventing advice."
            ),
            evidence_refs=_dedupe(context.evidence_refs),
        )
    detail = "unknown error" if last_error is None else str(last_error)
    raise RubricGenerationError(
        "evaluation rubric could not resolve a supported primary metric: " + detail
    ) from last_error


__all__ = [
    "DEFAULT_METRIC_CAPABILITIES",
    "MetricCapability",
    "MetricCapabilityRegistry",
    "RubricGenerationError",
    "assemble_evaluation_policy",
    "generate_evaluation_policy",
    "normalize_metric_name",
]
