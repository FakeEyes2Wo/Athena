"""Safe facade for projecting canonical research state into documents."""

import copy
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from athena.core.research_tree import ResearchTree
from athena.research.evaluation.spec import load_evaluator_spec, load_metric_json
from athena.research.experiment_documents.models import (
    Direction,
    MetricRecord,
    ProvenanceRecord,
    ReasonRecord,
    StageRecord,
)
from athena.research.experiment_documents.store import (
    DocumentStore,
    ProjectionBatch,
)
from athena.research.report import build_final_report, build_optimization_report

logger = logging.getLogger(__name__)

_TERMINAL_EXPERIMENTS = {"SUCCEEDED", "FAILED", "CANCELLED"}
_TERMINAL_VALIDATIONS = {"COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED"}


def _resolve_metric(
    roots: Iterable[Path], task_understanding: Mapping[str, object] | None
) -> str:
    """Resolve the evaluator metric, then fall back to task understanding."""
    for root in roots:
        if not (root / "metric.json").is_file():
            continue
        try:
            payload = load_metric_json(root)
            if "task_id" in payload:
                spec = load_evaluator_spec(root, legacy_ok=False)
                if spec is None:
                    raise ValueError("evaluator metric is missing")
                name = spec.primary_metric.strip()
            else:
                value = payload.get("primary_metric")
                if not isinstance(value, str):
                    raise ValueError("primary_metric is blank or missing")
                name = value.strip()
            if not name:
                raise ValueError("primary_metric is blank or missing")
            return name
        except (OSError, TypeError, ValueError):
            logger.warning("invalid evaluator metric at %s", root, exc_info=True)
    if isinstance(task_understanding, Mapping):
        value = task_understanding.get("primary_metric")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "primary"


def _experiment_record(
    experiment_id: str,
    experiment: Mapping[str, object],
    *,
    stage: Literal["baseline", "search"],
    metric_name: str,
    direction: Direction,
) -> StageRecord:
    """Build a projection record from one terminal canonical experiment."""
    evaluation = experiment.get("eval") or {}
    if not isinstance(evaluation, Mapping):
        evaluation = {}
    raw_artifacts = experiment.get("artifacts") or {}
    artifacts = raw_artifacts if isinstance(raw_artifacts, Mapping) else {}
    raw_error = experiment.get("error")
    summary = str(raw_error) if raw_error else "Recovered from canonical ResearchTree."
    return StageRecord(
        run_id=experiment_id,
        stage=stage,
        status=str(experiment["status"]),
        metric=MetricRecord(
            name=metric_name,
            direction=direction,
            primary=evaluation.get("primary"),
            reference=None,
            generalization_gap=None,
            secondary=evaluation.get("secondary") or {},
        ),
        artifacts=dict(artifacts),
        reason=ReasonRecord(
            kind="reconstructed_from_canonical_state",
            summary=summary,
        ),
        provenance=ProvenanceRecord(
            experiment_id=experiment_id,
            hypothesis_id=str(experiment["hypothesis_id"]),
            commit=str(experiment["commit"]),
        ),
    )


def _validation_record(
    validation: Mapping[str, object],
    *,
    sota: StageRecord | None,
    metric_name: str,
    direction: Direction,
) -> StageRecord | None:
    """Build a final record only from an explicitly terminal validation result."""
    status = str(validation.get("status", ""))
    if status not in _TERMINAL_VALIDATIONS:
        return None
    result_id = validation.get("result_id")
    final_score = validation.get("final_test_score")
    search_score = validation.get("test_score")
    if not result_id:
        if final_score is None and search_score is None:
            return None
        result_id = "final"
    if not isinstance(result_id, str) or not result_id.strip():
        return None
    raw_artifacts = {
        key.removesuffix("_ref"): value
        for key, value in validation.items()
        if key.endswith("_ref") and isinstance(value, str)
    }
    raw_secondary = validation.get("secondary") or {}
    secondary = raw_secondary if isinstance(raw_secondary, Mapping) else {}
    persisted_sota_commit = validation.get("sota_commit")
    sota_commit = (
        persisted_sota_commit
        if isinstance(persisted_sota_commit, str) and persisted_sota_commit.strip()
        else (sota.provenance.commit if sota else None)
    )
    provenance = ProvenanceRecord(
        sota_experiment_id=sota.provenance.experiment_id if sota else None,
        sota_commit=sota_commit,
        validation_commit=(
            str(validation["validation_commit"])
            if validation.get("validation_commit")
            else None
        ),
    )
    return StageRecord(
        run_id=result_id,
        stage="final",
        status=status,
        metric=MetricRecord(
            name=metric_name,
            direction=direction,
            primary=final_score if final_score is not None else search_score,
            reference=search_score,
            generalization_gap=validation.get("generalization_gap"),
            secondary=dict(secondary),
        ),
        artifacts=raw_artifacts,
        reason=ReasonRecord(
            kind="validated_result",
            summary="Reconstructed from canonical validation state.",
        ),
        provenance=provenance,
    )


class _ProjectionState(Protocol):
    validation: Mapping[str, object] | None
    validation_skipped: bool | None
    task_understanding: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ProjectionContext:
    """Canonical inputs shared by stage projection and recovery rebuild."""

    tree: ResearchTree
    state: _ProjectionState
    direction: Direction

    @property
    def validation(self) -> Mapping[str, object] | None:
        return self.state.validation

    @property
    def validation_skipped(self) -> bool:
        return bool(self.state.validation_skipped)

    @property
    def task_understanding(self) -> Mapping[str, object] | None:
        return self.state.task_understanding


class DocumentProjector(Protocol):
    """Public interface for safe experiment-document projections."""

    def project(self, event: Mapping[str, object], context: ProjectionContext) -> bool:
        """Project one validated stage event and report whether it committed."""
        raise NotImplementedError

    def rebuild(self, context: ProjectionContext) -> bool:
        """Rebuild documents from canonical state after a recovery."""
        raise NotImplementedError


class ExperimentDocumentProjector:
    """Project stage records while containing document-side failures."""

    def __init__(self, document_root: Path, evaluator_roots: Iterable[Path]) -> None:
        self._store = DocumentStore(Path(document_root))
        self._evaluator_roots = tuple(Path(root) for root in evaluator_roots)

    def project(self, event: Mapping[str, object], context: ProjectionContext) -> bool:
        """Render and atomically store one stage."""
        event_copy: Mapping[str, object] | None = None
        try:
            event_copy = copy.deepcopy(dict(event))
            tree_copy = ResearchTree.from_dict(copy.deepcopy(context.tree.to_dict()))
            validation_copy = (
                copy.deepcopy(dict(context.validation)) if context.validation else None
            )
            task_copy = (
                copy.deepcopy(dict(context.task_understanding))
                if context.task_understanding
                else None
            )
            metric_name = _resolve_metric(self._evaluator_roots, task_copy)
            record = StageRecord.from_event(
                event_copy, name=metric_name, direction=context.direction
            )
            batch = ProjectionBatch(
                kind="stage",
                records=(record,),
                aliases={record.stage: record.run_id},
                reports={
                    "FINAL_REPORT.md": build_final_report(
                        tree_copy,
                        validation_copy,
                        validation_skipped=context.validation_skipped,
                    ).encode("utf-8"),
                    "OPTIMIZATION.md": build_optimization_report(
                        tree_copy,
                        validation_copy,
                        metric_name=metric_name,
                        direction=context.direction,
                        validation_skipped=context.validation_skipped,
                    ).encode("utf-8"),
                },
            )
            self._store.commit(batch)
            return True
        except Exception:
            payload = event_copy if event_copy is not None else event
            getter = getattr(payload, "get", None)
            stage = getter("stage") if callable(getter) else None
            run_id = getter("run_id") if callable(getter) else None
            logger.warning(
                "document projection failed for stage=%r run_id=%r",
                stage,
                run_id,
                exc_info=True,
            )
            return False

    def rebuild(self, context: ProjectionContext) -> bool:
        """Reconstruct derived documents from a snapshot of canonical state."""
        try:
            tree_copy = ResearchTree.from_dict(copy.deepcopy(context.tree.to_dict()))
            validation_copy = (
                copy.deepcopy(dict(context.validation)) if context.validation else None
            )
            task_copy = (
                copy.deepcopy(dict(context.task_understanding))
                if context.task_understanding
                else None
            )
            data = tree_copy.to_dict()
            experiments = data.get("experiments") or {}
            metric_name = _resolve_metric(self._evaluator_roots, task_copy)
            records_for_store: list[StageRecord] = []
            aliases: dict[str, str] = {}
            records: dict[str, StageRecord] = {}
            if isinstance(experiments, Mapping):
                for experiment_id, experiment in experiments.items():
                    if not isinstance(experiment_id, str) or not isinstance(
                        experiment, Mapping
                    ):
                        continue
                    status = str(experiment.get("status", ""))
                    plan = experiment.get("plan") or {}
                    kind = plan.get("kind") if isinstance(plan, Mapping) else None
                    if kind not in {"baseline", "search"}:
                        continue
                    if status not in _TERMINAL_EXPERIMENTS:
                        continue
                    record = _experiment_record(
                        experiment_id,
                        experiment,
                        stage=kind,
                        metric_name=metric_name,
                        direction=context.direction,
                    )
                    records[experiment_id] = record
                    records_for_store.append(record)
                    if kind == "baseline" and experiment_id == "exp_baseline":
                        aliases["baseline"] = experiment_id
                    if kind == "search":
                        aliases["search"] = experiment_id

            sota_record = records.get(data.get("sota_id"))
            if context.validation_skipped:
                if sota_record is not None:
                    final_record = StageRecord(
                        run_id="final-skipped",
                        stage="final",
                        status="SKIPPED",
                        metric=MetricRecord(
                            name=metric_name,
                            direction=context.direction,
                            primary=None,
                            reference=sota_record.metric.primary,
                            generalization_gap=None,
                            secondary={},
                        ),
                        artifacts={},
                        reason=ReasonRecord(
                            kind="validation_skipped",
                            summary="Validation was explicitly skipped.",
                        ),
                        provenance=ProvenanceRecord(
                            sota_experiment_id=sota_record.run_id,
                            sota_commit=sota_record.provenance.commit,
                        ),
                    )
                else:
                    final_record = None
            elif isinstance(validation_copy, Mapping):
                final_record = _validation_record(
                    validation_copy,
                    sota=sota_record,
                    metric_name=metric_name,
                    direction=context.direction,
                )
            else:
                final_record = None
            if final_record is not None:
                records_for_store.append(final_record)
                aliases["final"] = final_record.run_id

            if not records_for_store and not aliases and not self._store.root.exists():
                return True

            batch = ProjectionBatch(
                kind="rebuild",
                records=tuple(records_for_store),
                aliases=aliases,
                reports={
                    "FINAL_REPORT.md": build_final_report(
                        tree_copy,
                        validation_copy,
                        validation_skipped=context.validation_skipped,
                    ).encode("utf-8"),
                    "OPTIMIZATION.md": build_optimization_report(
                        tree_copy,
                        validation_copy,
                        metric_name=metric_name,
                        direction=context.direction,
                        validation_skipped=context.validation_skipped,
                    ).encode("utf-8"),
                },
            )
            self._store.commit(batch)
            return True
        except Exception:
            logger.warning("document rebuild failed", exc_info=True)
            return False


__all__ = [
    "DocumentProjector",
    "ExperimentDocumentProjector",
    "ProjectionContext",
]
