"""Canonical ResearchTree v2 experiment graph."""

import json
import math
from collections import deque
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field, model_validator

from athena.core.contracts import ArtifactRef, CommitHash, new_id
from athena.core.persistence import atomic_write_json
from athena.core.research_models import (
    ComparisonVerdict,
    EvalResult,
    ExperimentPlan,
    Hypothesis,
    HypothesisStatus,
)
from athena.core.workspace import GitWorkBranch

SAVE_VERSION = 3
_LOAD_VERSIONS = {2, SAVE_VERSION}


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
        """登记假设；重复 id 时报错，返回规范化后的 id。"""
        hypothesis_id = hypothesis.id or new_id("hyp")
        if hypothesis_id in self._hypotheses:
            raise ValueError(f"duplicate hypothesis id: {hypothesis_id}")
        if (
            hypothesis.parent_id is not None
            and hypothesis.parent_id not in self._experiments
        ):
            raise KeyError(f"unknown parent experiment id: {hypothesis.parent_id}")
        order = hypothesis.order
        if order is None:
            order = (
                max(
                    (
                        existing.order
                        for existing in self._hypotheses.values()
                        if existing.order is not None
                    ),
                    default=-1,
                )
                + 1
            )
        elif any(existing.order == order for existing in self._hypotheses.values()):
            raise ValueError(f"duplicate hypothesis order: {order}")
        stored = Hypothesis.model_validate(
            {**hypothesis.model_dump(), "id": hypothesis_id, "order": order}
        )
        self._validate_supersedes(stored, stored.parent_id)
        self._hypotheses[hypothesis_id] = stored
        return hypothesis_id

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        """按 id 取假设；未知 id 报 KeyError。"""
        try:
            return self._hypotheses[hypothesis_id]
        except KeyError as exc:
            raise KeyError(f"unknown hypothesis id: {hypothesis_id}") from exc

    def pending_hypotheses(self) -> list[Hypothesis]:
        """返回状态为 PROPOSED 的待选假设。"""
        return [
            hypothesis
            for hypothesis in self._hypotheses.values()
            if hypothesis.status == "PROPOSED"
        ]

    def update_hypothesis_status(
        self, hypothesis_id: str, status: HypothesisStatus
    ) -> None:
        """原地更新假设状态。"""
        hypothesis = self.get_hypothesis(hypothesis_id)
        self._hypotheses[hypothesis_id] = Hypothesis.model_validate(
            {**hypothesis.model_dump(), "status": status}
        )

    def add_experiment(self, experiment_id: str, experiment: Experiment) -> None:
        """登记实验记录并维护父子索引；重复 id / 未知假设或父实验时报错。"""
        if not experiment_id.strip():
            raise ValueError("experiment id must be nonblank")
        if experiment_id in self._experiments:
            raise ValueError(f"duplicate experiment id: {experiment_id}")
        if experiment.hypothesis_id not in self._hypotheses:
            raise KeyError(f"unknown hypothesis id: {experiment.hypothesis_id}")
        existing_experiment_id = self.experiment_for_hypothesis(
            experiment.hypothesis_id
        )
        if existing_experiment_id is not None:
            raise ValueError(
                f"hypothesis {experiment.hypothesis_id} already has an experiment: "
                f"{existing_experiment_id}"
            )
        if (
            experiment.parent_id is not None
            and experiment.parent_id not in self._experiments
        ):
            raise KeyError(f"unknown parent experiment id: {experiment.parent_id}")
        hypothesis = self._hypotheses[experiment.hypothesis_id]
        if experiment.parent_id != hypothesis.parent_id:
            raise ValueError(
                f"experiment parent {experiment.parent_id!r} does not match "
                f"hypothesis parent {hypothesis.parent_id!r}"
            )

        self._experiments[experiment_id] = experiment
        self._children_index.setdefault(experiment_id, [])
        if experiment.parent_id is not None:
            self._children_index.setdefault(experiment.parent_id, []).append(
                experiment_id
            )

    def get_experiment(self, experiment_id: str) -> Experiment:
        """按 id 取实验记录；未知 id 报 KeyError。"""
        try:
            return self._experiments[experiment_id]
        except KeyError as exc:
            raise KeyError(f"unknown experiment id: {experiment_id}") from exc

    def root_experiment_ids(self) -> list[str]:
        """返回无父实验的根实验 id 列表。"""
        return [
            experiment_id
            for experiment_id, experiment in self._experiments.items()
            if experiment.parent_id is None
        ]

    def list_children(self, experiment_id: str) -> list[str]:
        """返回某实验的直接子实验 id 列表。"""
        self.get_experiment(experiment_id)
        return list(self._children_index.get(experiment_id, ()))

    def list_descendants(self, experiment_id: str) -> list[str]:
        """BFS 返回某实验的全部后代实验 id。"""
        self.get_experiment(experiment_id)
        descendants: list[str] = []
        queue = deque(self._children_index.get(experiment_id, ()))
        while queue:
            child_id = queue.popleft()
            descendants.append(child_id)
            queue.extend(self._children_index.get(child_id, ()))
        return descendants

    def experiment_path(self, experiment_id: str) -> list[str]:
        """从根到该实验的祖先链（含自身）；存在循环时报错。"""
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
        """按祖先链顺序返回每级实验对应的假设。"""
        return [
            self.get_hypothesis(self.get_experiment(item_id).hypothesis_id)
            for item_id in self.experiment_path(experiment_id)
        ]

    def experiment_for_hypothesis(self, hypothesis_id: str) -> str | None:
        """Return the experiment registered for a hypothesis, if one exists."""
        for experiment_id, experiment in self._experiments.items():
            if experiment.hypothesis_id == hypothesis_id:
                return experiment_id
        return None

    def experiments(self, kind: str | None = None) -> list[Experiment]:
        """Return experiment records, optionally filtered to one Plan kind."""
        if kind is None:
            return list(self._experiments.values())
        return [
            experiment
            for experiment in self._experiments.values()
            if experiment.plan.kind == kind
        ]

    def active_hypotheses(
        self, experiment_id: str, child: Hypothesis
    ) -> list[Hypothesis]:
        """Return selected ancestry minus superseded claims, followed by child."""
        if child.parent_id != experiment_id:
            raise ValueError(
                "child parent experiment does not match selected parent experiment"
            )
        ancestors = self.hypotheses_path(experiment_id)
        self._validate_supersedes(child, experiment_id, ancestors=ancestors)
        superseded = set(child.supersedes)
        return [
            *(
                hypothesis
                for hypothesis in ancestors
                if hypothesis.id not in superseded
            ),
            child,
        ]

    def _validate_supersedes(
        self,
        child: Hypothesis,
        experiment_id: str | None,
        *,
        ancestors: list[Hypothesis] | None = None,
    ) -> None:
        if len(child.supersedes) != len(set(child.supersedes)):
            raise ValueError("supersedes contains duplicate hypothesis ids")
        if child.id is not None and child.id in child.supersedes:
            raise ValueError("supersedes cannot include the child hypothesis")
        if not child.supersedes:
            return
        if experiment_id is None:
            raise ValueError(
                "supersedes ids must be on the selected parent experiment path"
            )
        lineage = ancestors or self.hypotheses_path(experiment_id)
        ancestor_ids = {hypothesis.id for hypothesis in lineage}
        if any(item not in ancestor_ids for item in child.supersedes):
            raise ValueError(
                "every supersedes id must be on the selected parent experiment path"
            )

    def transition_experiment(
        self,
        experiment_id: str,
        status: ExperimentStatus,
        *,
        error: str | None = None,
    ) -> None:
        """按允许的转移表推进实验状态；非法转移或终态报错。"""
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
        """把 RUNNING 实验置为 SUCCEEDED 并写入评估与产物。"""
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
        """为实验记录附加一个产物引用；kind 非空。"""
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
        """把成功的 baseline/search 实验标记为 SOTA。"""
        experiment = self.get_experiment(experiment_id)
        if experiment.status is not ExperimentStatus.SUCCEEDED:
            raise ValueError("SOTA requires a successful experiment")
        if experiment.plan.kind not in {"baseline", "search"}:
            raise ValueError("experiment kind is not eligible for SOTA")
        self._sota_id = experiment_id

    def best_experiment_id(self) -> str | None:
        """返回当前 SOTA 实验 id；尚无则为 None。"""
        return self._sota_id

    def to_dict(self) -> dict[str, Any]:
        """导出为带版本的 JSON 兼容 dict。"""
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
        """从 dict 重建树并校验版本/字段/父子关系与 SOTA。"""
        version = payload.get("version")
        if version not in _LOAD_VERSIONS:
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

        parsed_hypotheses: list[tuple[str, Hypothesis]] = []
        explicit_orders: set[int] = set()
        for hypothesis_id, raw_hypothesis in raw_hypotheses.items():
            if not isinstance(hypothesis_id, str) or not hypothesis_id.strip():
                raise ValueError("hypothesis mapping keys must be nonblank strings")
            hypothesis = Hypothesis.model_validate(raw_hypothesis)
            if hypothesis.id != hypothesis_id:
                raise ValueError(
                    f"hypothesis id does not match mapping key: {hypothesis_id}"
                )
            order = hypothesis.order
            if order is None and version == SAVE_VERSION:
                raise ValueError(
                    f"research tree v{SAVE_VERSION} hypothesis order is required"
                )
            if order is not None and order in explicit_orders:
                raise ValueError(f"duplicate hypothesis order: {order}")
            if order is not None:
                explicit_orders.add(order)
            parsed_hypotheses.append((hypothesis_id, hypothesis))

        hypotheses: dict[str, Hypothesis] = {}
        used_orders = set(explicit_orders)
        next_order = 0
        for hypothesis_id, hypothesis in parsed_hypotheses:
            if hypothesis.order is None:
                while next_order in used_orders:
                    next_order += 1
                hypothesis = Hypothesis.model_validate(
                    {**hypothesis.model_dump(), "order": next_order}
                )
                used_orders.add(next_order)
                next_order += 1
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
        for hypothesis in hypotheses.values():
            if (
                hypothesis.parent_id is not None
                and hypothesis.parent_id not in experiments
            ):
                raise KeyError(f"unknown parent experiment id: {hypothesis.parent_id}")
        for experiment_id, experiment in experiments.items():
            hypothesis = hypotheses[experiment.hypothesis_id]
            if experiment.parent_id != hypothesis.parent_id:
                raise ValueError(
                    f"experiment {experiment_id} parent {experiment.parent_id!r} "
                    "does not match hypothesis parent "
                    f"{hypothesis.parent_id!r}"
                )

        sota_id = payload["sota_id"]
        if sota_id is not None and not isinstance(sota_id, str):
            raise ValueError("sota_id must be a string or null")

        tree = cls()
        tree._hypotheses = hypotheses
        tree._experiments = experiments
        tree._children_index = children_index
        for hypothesis in hypotheses.values():
            tree._validate_supersedes(hypothesis, hypothesis.parent_id)
        if sota_id is not None:
            tree.set_sota(sota_id)
        return tree

    @staticmethod
    def _validate_and_build_children(
        hypotheses: Mapping[str, Hypothesis],
        experiments: Mapping[str, Experiment],
    ) -> dict[str, list[str]]:
        children = {experiment_id: [] for experiment_id in experiments}
        hypothesis_experiments: dict[str, str] = {}
        for experiment_id, experiment in experiments.items():
            if experiment.hypothesis_id not in hypotheses:
                raise KeyError(f"unknown hypothesis id: {experiment.hypothesis_id}")
            existing_experiment_id = hypothesis_experiments.get(
                experiment.hypothesis_id
            )
            if existing_experiment_id is not None:
                raise ValueError(
                    f"hypothesis {experiment.hypothesis_id} already has an "
                    f"experiment: {existing_experiment_id}"
                )
            hypothesis_experiments[experiment.hypothesis_id] = experiment_id
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
        """原子写 JSON 到目标路径，返回目标路径。"""
        return atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> "ResearchTree":
        """从 JSON 文件加载并重建树；根须为对象。"""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("research tree payload must be an object")
        return cls.from_dict(payload)
