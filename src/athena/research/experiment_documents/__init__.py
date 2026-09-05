"""Canonical public API for experiment-document projections."""

from .projector import DocumentProjector, ExperimentDocumentProjector, ProjectionContext

__all__ = [
    "DocumentProjector",
    "ExperimentDocumentProjector",
    "ProjectionContext",
]
