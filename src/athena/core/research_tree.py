"""Canonical ResearchTree v2 experiment graph."""

import json
import math
import os
from collections import deque
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field, model_validator

from athena.core.contracts import ArtifactRef, CommitHash, new_id
from athena.core.research_models import (
    ExperimentPlan,
    Hypothesis,
    HypothesisStatus,
)
from athena.core.workspace import GitWorkBranch
from athena.evaluation.types import ComparisonVerdict, EvalResult

SAVE_VERSION = 2


class ExperimentStatus(StrEnum):
    """Lifecycle states for one experiment execution."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Experiment(BaseModel):
    """One execution record; its mapping key is the experiment ID."""

    parent_id: str | None = None
    hypothesis_id: str
    commit: CommitHash
    plan: ExperimentPlan
    gitwork: GitWorkBranch
    status: ExperimentStatus = ExperimentStatus.PENDING
    eval: EvalResult | None = None
    verdict: ComparisonVerdict | None = None
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)
    error: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_payload(self) -> "Experiment":
        if not self.gitwork.path.strip() or not self.gitwork.branch.strip():
            raise ValueError("experiment worktree path and branch must be nonblank")
        if self.status is ExperimentStatus.SUCCEEDED:
            if self.eval is None:
                raise ValueError("successful experiment requires evaluation")
            if not math.isfinite(self.eval.primary):
                raise ValueError("successful experiment primary metric must be finite")
            if not self.eval.per_sample.strip():
                raise ValueError("successful experiment requires per-sample evidence")
        elif self.eval is not None or self.verdict is not None:
            raise ValueError(
                "only successful experiments may contain evaluation results"
            )

        if self.status is ExperimentStatus.FAILED:
            if self.error is None or not self.error.strip():
                raise ValueError("failed experiment requires a nonblank error")
        elif self.error is not None:
            raise ValueError("only failed experiments may contain an error")
        return self


_ALLOWED_TRANSITIONS: dict[ExperimentStatus, set[ExperimentStatus]] = {
    ExperimentStatus.PENDING: {
        ExperimentStatus.RUNNING,
        ExperimentStatus.CANCELLED,
    },
    ExperimentStatus.RUNNING: {
        ExperimentStatus.SUCCEEDED,
        ExperimentStatus.FAILED,
        ExperimentStatus.CANCELLED,
    },
    ExperimentStatus.SUCCEEDED: set(),
    ExperimentStatus.FAILED: set(),
    ExperimentStatus.CANCELLED: set(),
}


class ResearchTree:
    """Own hypotheses, experiment records, relationships, and selected SOTA."""

    def __init__(self) -> None:
        self._hypotheses: dict[str, Hypothesis] = {}
        self._experiments: dict[str, Experiment] = {}
        self._children_index: dict[str, list[str]] = {}
        self._sota_id: str | None = None

    def add_hypothesis(self, hypothesis: Hypothesis) -> str:
        hypothesis_id = hypothesis.id or new_id("hyp")
        if hypothesis_id in self._hypotheses:
            raise ValueError(f"duplicate hypothesis id: {hypothesis_id}")
        stored = Hypothesis.model_validate(
            {**hypothesis.model_dump(), "id": hypothesis_id}
        )
        self._hypotheses[hypothesis_id] = stored
        return hypothesis_id

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        try:
            return self._hypotheses[hypothesis_id]
        except KeyError as exc:
            raise KeyError(f"unknown hypothesis id: {hypothesis_id}") from exc

    def pending_hypotheses(self) -> list[Hypothesis]:
        return [
            hypothesis
            for hypothesis in self._hypotheses.values()
            if hypothesis.status == "PROPOSED"
        ]

    def update_hypothesis_status(
        self, hypothesis_id: str, status: HypothesisStatus
    ) -> None:
        hypothesis = self.get_hypothesis(hypothesis_id)
        self._hypotheses[hypothesis_id] = Hypothesis.model_validate(
            {**hypothesis.model_dump(), "status": status}
        )

    def add_experiment(self, experiment_id: str, experiment: Experiment) -> None:
        if not experiment_id.strip():
            raise ValueError("experiment id must be nonblank")
        if experiment_id in self._experiments:
            raise ValueError(f"duplicate experiment id: {experiment_id}")
        if experiment.hypothesis_id not in self._hypotheses:
            raise KeyError(f"unknown hypothesis id: {experiment.hypothesis_id}")
        if (
            experiment.parent_id is not None
            and experiment.parent_id not in self._experiments
        ):
            raise KeyError(f"unknown parent experiment id: {experiment.parent_id}")

        self._experiments[experiment_id] = experiment
        self._children_index.setdefault(experiment_id, [])
        if experiment.parent_id is not None:
            self._children_index.setdefault(experiment.parent_id, []).append(
                experiment_id
            )

    def get_experiment(self, experiment_id: str) -> Experiment:
        try:
            return self._experiments[experiment_id]
        except KeyError as exc:
            raise KeyError(f"unknown experiment id: {experiment_id}") from exc

    def root_experiment_ids(self) -> list[str]:
        return [
            experiment_id
            for experiment_id, experiment in self._experiments.items()
            if experiment.parent_id is None
        ]

    def list_children(self, experiment_id: str) -> list[str]:
        self.get_experiment(experiment_id)
        return list(self._children_index.get(experiment_id, ()))

    def list_descendants(self, experiment_id: str) -> list[str]:
        self.get_experiment(experiment_id)
        descendants: list[str] = []
        queue = deque(self._children_index.get(experiment_id, ()))
        while queue:
            child_id = queue.popleft()
            descendants.append(child_id)
            queue.extend(self._children_index.get(child_id, ()))
        return descendants

    def experiment_path(self, experiment_id: str) -> list[str]:
        path: list[str] = []
        visited: set[str] = set()
        current_id: str | None = experiment_id
        while current_id is not None:
            if current_id in visited:
                raise ValueError("experiment parent cycle")
            visited.add(current_id)
            experiment = self.get_experiment(current_id)
            path.append(current_id)
            current_id = experiment.parent_id
        return list(reversed(path))

    def hypotheses_path(self, experiment_id: str) -> list[Hypothesis]:
        return [
            self.get_hypothesis(self.get_experiment(item_id).hypothesis_id)
            for item_id in self.experiment_path(experiment_id)
        ]

    def transition_experiment(
        self,
        experiment_id: str,
        status: ExperimentStatus,
        *,
        error: str | None = None,
    ) -> None:
        experiment = self.get_experiment(experiment_id)
        if not _ALLOWED_TRANSITIONS[experiment.status]:
            raise ValueError(f"experiment status is terminal: {experiment.status}")
        if status not in _ALLOWED_TRANSITIONS[experiment.status]:
            raise ValueError(
                f"invalid experiment transition: {experiment.status} -> {status}"
            )
        if status is ExperimentStatus.FAILED and (error is None or not error.strip()):
            raise ValueError("FAILED transition requires a nonblank error")
        if status is not ExperimentStatus.FAILED and error is not None:
            raise ValueError("only FAILED transition accepts an error")
        self._experiments[experiment_id] = Experiment.model_validate(
            {**experiment.model_dump(), "status": status, "error": error}
        )

    def complete_experiment(
        self,
        experiment_id: str,
        *,
        eval: EvalResult,
        verdict: ComparisonVerdict | None,
        artifacts: dict[str, ArtifactRef],
        commit: CommitHash | None = None,
    ) -> None:
        experiment = self.get_experiment(experiment_id)
        if experiment.status is not ExperimentStatus.RUNNING:
            raise ValueError("only a running experiment can complete")
        if eval.experiment_id != experiment_id:
            raise ValueError("evaluation experiment id does not match record key")
        if not math.isfinite(eval.primary):
            raise ValueError("evaluation primary metric must be finite")
        merged_artifacts = {**experiment.artifacts, **artifacts}
        self._experiments[experiment_id] = Experiment.model_validate(
            {
                **experiment.model_dump(),
                "status": ExperimentStatus.SUCCEEDED,
                "eval": eval.model_dump(),
                "verdict": verdict.model_dump() if verdict is not None else None,
                "artifacts": merged_artifacts,
                "commit": commit or experiment.commit,
                "error": None,
            }
        )

    def attach_artifact(self, experiment_id: str, kind: str, ref: ArtifactRef) -> None:
        if not kind.strip():
            raise ValueError("artifact kind must be nonblank")
        experiment = self.get_experiment(experiment_id)
        self._experiments[experiment_id] = Experiment.model_validate(
            {
                **experiment.model_dump(),
                "artifacts": {**experiment.artifacts, kind: ref},
            }
        )

    def set_sota(self, experiment_id: str) -> None:
        experiment = self.get_experiment(experiment_id)
        if experiment.status is not ExperimentStatus.SUCCEEDED:
            raise ValueError("SOTA requires a successful experiment")
        if experiment.plan.kind not in {"baseline", "search"}:
            raise ValueError("experiment kind is not eligible for SOTA")
        self._sota_id = experiment_id

    def best_experiment_id(self) -> str | None:
        return self._sota_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SAVE_VERSION,
            "sota_id": self._sota_id,
            "hypotheses": {
                hypothesis_id: hypothesis.model_dump(mode="json")
                for hypothesis_id, hypothesis in self._hypotheses.items()
            },
            "experiments": {
                experiment_id: experiment.model_dump(mode="json")
                for experiment_id, experiment in self._experiments.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ResearchTree":
        version = payload.get("version")
        if version != SAVE_VERSION:
            raise ValueError(f"unsupported research tree version: {version}")

        expected_fields = {"version", "sota_id", "hypotheses", "experiments"}
        if set(payload) != expected_fields:
            raise ValueError(
                "invalid research tree top-level fields: "
                f"expected {sorted(expected_fields)}"
            )

        raw_hypotheses = payload["hypotheses"]
        raw_experiments = payload["experiments"]
        if not isinstance(raw_hypotheses, Mapping):
            raise ValueError("research tree hypotheses must be a mapping")
        if not isinstance(raw_experiments, Mapping):
            raise ValueError("research tree experiments must be a mapping")

        hypotheses: dict[str, Hypothesis] = {}
        for hypothesis_id, raw_hypothesis in raw_hypotheses.items():
            if not isinstance(hypothesis_id, str) or not hypothesis_id.strip():
                raise ValueError("hypothesis mapping keys must be nonblank strings")
            hypothesis = Hypothesis.model_validate(raw_hypothesis)
            if hypothesis.id != hypothesis_id:
                raise ValueError(
                    f"hypothesis id does not match mapping key: {hypothesis_id}"
                )
            hypotheses[hypothesis_id] = hypothesis

        experiments: dict[str, Experiment] = {}
        for experiment_id, raw_experiment in raw_experiments.items():
            if not isinstance(experiment_id, str) or not experiment_id.strip():
                raise ValueError("experiment mapping keys must be nonblank strings")
            experiment = Experiment.model_validate(raw_experiment)
            if (
                experiment.eval is not None
                and experiment.eval.experiment_id != experiment_id
            ):
                raise ValueError(
                    f"evaluation experiment id does not match mapping key: {experiment_id}"
                )
            experiments[experiment_id] = experiment

        children_index = cls._validate_and_build_children(hypotheses, experiments)

        sota_id = payload["sota_id"]
        if sota_id is not None and not isinstance(sota_id, str):
            raise ValueError("sota_id must be a string or null")

        tree = cls()
        tree._hypotheses = hypotheses
        tree._experiments = experiments
        tree._children_index = children_index
        if sota_id is not None:
            tree.set_sota(sota_id)
        return tree

    @staticmethod
    def _validate_and_build_children(
        hypotheses: Mapping[str, Hypothesis],
        experiments: Mapping[str, Experiment],
    ) -> dict[str, list[str]]:
        children = {experiment_id: [] for experiment_id in experiments}
        for experiment_id, experiment in experiments.items():
            if experiment.hypothesis_id not in hypotheses:
                raise KeyError(f"unknown hypothesis id: {experiment.hypothesis_id}")
            if experiment.parent_id is not None:
                if experiment.parent_id not in experiments:
                    raise KeyError(
                        f"unknown parent experiment id: {experiment.parent_id}"
                    )
                children[experiment.parent_id].append(experiment_id)

        for experiment_id in experiments:
            visited: set[str] = set()
            current_id: str | None = experiment_id
            while current_id is not None:
                if current_id in visited:
                    raise ValueError("experiment parent cycle")
                visited.add(current_id)
                current_id = experiments[current_id].parent_id
        return children

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    self.to_dict(),
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ResearchTree":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("research tree payload must be an object")
        return cls.from_dict(payload)
