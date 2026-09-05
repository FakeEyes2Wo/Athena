"""Canonical public API for experiment-document projections."""

from .models import ProjectionOutcome, StageEvent
from .projector import DocumentProjector, ExperimentDocumentProjector

__all__ = [
    "DocumentProjector",
    "ExperimentDocumentProjector",
    "ProjectionOutcome",
    "StageEvent",
]
