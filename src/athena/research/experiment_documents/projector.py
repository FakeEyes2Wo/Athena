"""Safe facade for projecting canonical research state into documents."""

import copy
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from athena.core.research_tree import ResearchTree
from athena.research.experiment_documents.metric import MetricResolver
from athena.research.experiment_documents.models import (
    Direction,
    ProjectionOutcome,
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
        """Return a safe stale result until the rebuild projection is implemented."""
        try:
            raise NotImplementedError("rebuild is implemented by Task 6")
        except Exception:
            logger.warning("document rebuild failed", exc_info=True)
            return ProjectionOutcome.stale()


__all__ = ["DocumentProjector", "ExperimentDocumentProjector"]
