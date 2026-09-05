"""Safe facade for projecting canonical research state into documents."""

import copy
import hashlib
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal, Protocol

from athena.core.research_tree import ResearchTree
from athena.research.experiment_documents.metric import MetricResolver
from athena.research.experiment_documents.models import (
    Direction,
    MetricRecord,
    ProvenanceRecord,
    ProjectionOutcome,
    ReasonRecord,
    StageEvent,
    StageRecord,
)
from athena.research.experiment_documents.render import (
    render_final_report,
    render_optimization_report,
    render_stage_record,
)
from athena.research.experiment_documents.store import (
    DocumentStore,
    ProjectionBatch,
    RunCandidate,
)

logger = logging.getLogger(__name__)

_TERMINAL_EXPERIMENTS = {"SUCCEEDED", "FAILED", "CANCELLED"}
_TERMINAL_VALIDATIONS = {"COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED"}
NOOP_PROJECTION_ID = hashlib.sha256(b"athena:experiment-documents:no-op:v1").hexdigest()


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
        schema_version=1,
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
            phase="PREPARE" if stage == "baseline" else "SEARCH",
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
        key: value
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
        phase="VALIDATE",
        sota_experiment_id=sota.provenance.experiment_id if sota else None,
        sota_commit=sota_commit,
        validation_commit=(
            str(validation["validation_commit"])
            if validation.get("validation_commit")
            else None
        ),
    )
    return StageRecord(
        schema_version=1,
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


class DocumentProjector(Protocol):
    """Public interface for safe experiment-document projections."""

    def project_stage(
        self,
        event: Mapping[str, object],
        *,
        tree: ResearchTree,
        validation: Mapping[str, object] | None,
        validation_skipped: bool,
        task_understanding: Mapping[str, object] | None,
        direction: Direction,
    ) -> ProjectionOutcome:
        """Project one validated stage event and return a sanitized outcome."""
        raise NotImplementedError

    def rebuild(
        self,
        *,
        tree: ResearchTree,
        validation: Mapping[str, object] | None,
        validation_skipped: bool,
        task_understanding: Mapping[str, object] | None,
        direction: Direction,
    ) -> ProjectionOutcome:
        """Rebuild documents from canonical state after a recovery."""
        raise NotImplementedError


class ExperimentDocumentProjector:
    """Project stage records while containing document-side failures."""

    def __init__(self, document_root: Path, evaluator_roots: Iterable[Path]) -> None:
        self._store = DocumentStore(Path(document_root))
        self._metrics = MetricResolver(evaluator_roots)

    def project_stage(
        self,
        event: Mapping[str, object],
        *,
        tree: ResearchTree,
        validation: Mapping[str, object] | None,
        validation_skipped: bool,
        task_understanding: Mapping[str, object] | None,
        direction: Direction,
    ) -> ProjectionOutcome:
        """Render and atomically store one stage, returning stale on failure."""
        event_copy: Mapping[str, object] | None = None
        try:
            event_copy = copy.deepcopy(dict(event))
            tree_copy = ResearchTree.from_dict(copy.deepcopy(tree.to_dict()))
            validation_copy = copy.deepcopy(dict(validation)) if validation else None
            task_copy = (
                copy.deepcopy(dict(task_understanding)) if task_understanding else None
            )
            parsed = StageEvent.model_validate(event_copy)
            metric_name = self._metrics.resolve(task_copy)
            record = StageRecord.from_event(
                parsed, name=metric_name, direction=direction
            )
            record_bytes = render_stage_record(record)
            batch = ProjectionBatch(
                kind="stage",
                stage=record.stage,
                run_id=record.run_id,
                runs=(RunCandidate(record=record, content=record_bytes),),
                aliases={record.stage: record.run_id},
                reports={
                    "FINAL_REPORT.md": render_final_report(
                        tree_copy,
                        validation_copy,
                        validation_skipped=validation_skipped,
                    ),
                    "OPTIMIZATION.md": render_optimization_report(
                        tree_copy,
                        validation_copy,
                        metric_name=metric_name,
                        direction=direction,
                        validation_skipped=validation_skipped,
                    ),
                },
            )
            return ProjectionOutcome.success(self._store.commit(batch))
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
            return ProjectionOutcome.stale()

    def rebuild(
        self,
        *,
        tree: ResearchTree,
        validation: Mapping[str, object] | None,
        validation_skipped: bool,
        task_understanding: Mapping[str, object] | None,
        direction: Direction,
    ) -> ProjectionOutcome:
        """Reconstruct derived documents from a snapshot of canonical state."""
        try:
            tree_copy = ResearchTree.from_dict(copy.deepcopy(tree.to_dict()))
            validation_copy = copy.deepcopy(dict(validation)) if validation else None
            task_copy = (
                copy.deepcopy(dict(task_understanding)) if task_understanding else None
            )
            data = tree_copy.to_dict()
            experiments = data.get("experiments") or {}
            metric_name = self._metrics.resolve(task_copy)
            runs: list[RunCandidate] = []
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
                        direction=direction,
                    )
                    records[experiment_id] = record
                    runs.append(
                        RunCandidate(
                            record=record,
                            content=render_stage_record(record),
                            match_mode="recoverable",
                        )
                    )
                    if kind == "baseline" and experiment_id == "exp_baseline":
                        aliases["baseline"] = experiment_id
                    if kind == "search":
                        aliases["search"] = experiment_id

            sota_record = records.get(data.get("sota_id"))
            if validation_skipped:
                if sota_record is not None:
                    final_record = StageRecord(
                        schema_version=1,
                        run_id="final-skipped",
                        stage="final",
                        status="SKIPPED",
                        metric=MetricRecord(
                            name=metric_name,
                            direction=direction,
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
                            phase="VALIDATE",
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
                    direction=direction,
                )
            else:
                final_record = None
            if final_record is not None:
                runs.append(
                    RunCandidate(
                        record=final_record,
                        content=render_stage_record(final_record),
                        match_mode="recoverable",
                    )
                )
                aliases["final"] = final_record.run_id

            if not runs and not aliases and not self._store.root.exists():
                return ProjectionOutcome.success(NOOP_PROJECTION_ID)

            batch = ProjectionBatch(
                kind="rebuild",
                stage=None,
                run_id=None,
                runs=tuple(runs),
                aliases=aliases,
                reports={
                    "FINAL_REPORT.md": render_final_report(
                        tree_copy,
                        validation_copy,
                        validation_skipped=validation_skipped,
                    ),
                    "OPTIMIZATION.md": render_optimization_report(
                        tree_copy,
                        validation_copy,
                        metric_name=metric_name,
                        direction=direction,
                        validation_skipped=validation_skipped,
                    ),
                },
            )
            return ProjectionOutcome.success(self._store.commit(batch))
        except Exception:
            logger.warning("document rebuild failed", exc_info=True)
            return ProjectionOutcome.stale()


__all__ = ["DocumentProjector", "ExperimentDocumentProjector", "NOOP_PROJECTION_ID"]
