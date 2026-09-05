"""Resolve the metric used in projected experiment documents."""

import logging
from pathlib import Path
from typing import Iterable, Mapping

from athena.research.evaluation.spec import load_evaluator_spec, load_metric_json

logger = logging.getLogger(__name__)


class MetricResolver:
    """Resolve a metric name using the frozen evaluator authority order."""

    def __init__(self, evaluator_roots: Iterable[Path]) -> None:
        self._roots = tuple(Path(root) for root in evaluator_roots)

    def resolve(self, task_understanding: Mapping[str, object] | None) -> str:
        """Return evaluator, task-understanding, or literal fallback metric."""
        for root in self._roots:
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
                    name = payload.get("primary_metric")
                    if not isinstance(name, str):
                        raise ValueError("primary_metric is blank or missing")
                    name = name.strip()
                if not name:
                    raise ValueError("primary_metric is blank or missing")
                return name
            except (OSError, TypeError, ValueError):
                logger.warning("invalid evaluator metric at %s", root, exc_info=True)

        if isinstance(task_understanding, Mapping):
            name = task_understanding.get("primary_metric")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return "primary"


__all__ = ["MetricResolver"]
