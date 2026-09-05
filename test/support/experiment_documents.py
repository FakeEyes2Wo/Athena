"""Test doubles for the experiment-document projection boundary."""

from dataclasses import dataclass, field

from athena.research.experiment_documents import ProjectionContext


@dataclass
class RecordingDocumentProjector:
    """Record projection calls while returning a configurable outcome."""

    outcome: bool = True
    stage_calls: list[dict[str, object]] = field(default_factory=list)
    rebuild_calls: list[dict[str, object]] = field(default_factory=list)

    def project(self, event: dict[str, object], context: ProjectionContext) -> bool:
        """Record one stage event and its canonical context."""
        self.stage_calls.append(
            {
                "event": dict(event),
                "tree": context.tree,
                "validation": context.validation,
                "validation_skipped": context.validation_skipped,
                "task_understanding": context.task_understanding,
                "direction": context.direction,
            }
        )
        return self.outcome

    def rebuild(self, context: ProjectionContext) -> bool:
        """Record one rebuild request and return the configured outcome."""
        self.rebuild_calls.append(
            {
                "tree": context.tree,
                "validation": context.validation,
                "validation_skipped": context.validation_skipped,
                "task_understanding": context.task_understanding,
                "direction": context.direction,
            }
        )
        return self.outcome
