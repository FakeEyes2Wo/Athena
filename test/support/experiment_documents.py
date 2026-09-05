"""Test doubles for the experiment-document projection boundary."""

from dataclasses import dataclass, field
from typing import Any

from athena.research.experiment_documents import ProjectionOutcome


@dataclass
class RecordingDocumentProjector:
    """Record projection calls while returning a configurable outcome."""

    outcome: ProjectionOutcome = field(
        default_factory=lambda: ProjectionOutcome.success("a" * 64)
    )
    stage_calls: list[dict[str, object]] = field(default_factory=list)
    rebuild_calls: list[dict[str, object]] = field(default_factory=list)

    def project_stage(
        self, event: dict[str, object], **context: Any
    ) -> ProjectionOutcome:
        """Record one stage event and its canonical context."""
        self.stage_calls.append({"event": dict(event), **context})
        return self.outcome

    def rebuild(self, **context: Any) -> ProjectionOutcome:
        """Record one rebuild request and return the configured outcome."""
        self.rebuild_calls.append(context)
        return self.outcome
