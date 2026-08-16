"""Atomic persistence for the autonomous Supervisor's ``state.json``."""

import json
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from athena.core.persistence import atomic_write_json
from athena.research.supervisor.plans import PlanState


class ResearchState(BaseModel):
    """Small durable checkpoint for unfinished autonomous research."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["RUNNING", "WAITING", "COMPLETED", "STOPPED", "FAILED"]
    phase: Literal["PREPARE", "SEARCH", "VALIDATE", "COMPLETED"]
    search_limit: int = Field(ge=0)
    concurrency: int = Field(ge=1)
    # 每轮 ideation 的并行 lane 数与每 lane 假设数（与 SEARCH 并发度解耦）。
    ideator_count: int = Field(default=3, ge=1, le=8)
    hypotheses_per_ideator: int = Field(default=2, ge=1, le=5)
    # SEARCH 调度模式：False=自动按优先级出队；True=每个假设生成后等待人工选定。
    manual_mode: bool = False
    plans: dict[str, PlanState] = Field(default_factory=dict)
    validation: dict[str, object] | None = None
    # PREPARE 产出的 EDA 工作区目录（Ideator 自行探索）；仅路径元数据，非 EDA 结果。
    eda_dir: str | None = None
    # Supervisor 在首个 task-understanding turn 产出的结构化任务理解（供 GUI 意图预览）。
    task_understanding: dict[str, object] | None = None
    # Academic Survey 建好的论文语料索引；Ideator 只读，凭它调用检索算子。
    # 与其他引用一样落在本项目的 artifact store 里，换机器取不到时重跑即可。
    corpus_ref: str | None = None

    @model_validator(mode="after")
    def _validate_plan_keys(self) -> "ResearchState":
        for plan_id, plan in self.plans.items():
            if plan.kind == "PREPARE" and plan_id != "prepare":
                raise ValueError("PREPARE plan key must be 'prepare'")
            if plan.kind == "VALIDATE" and plan_id != "validate":
                raise ValueError("VALIDATE plan key must be 'validate'")
            if plan.kind == "SEARCH" and (
                not plan_id.strip() or plan_id in {"prepare", "validate"}
            ):
                raise ValueError("SEARCH plan key must be a Hypothesis ID")
        return self

    def save(self, path: str | Path) -> Path:
        """Atomically replace ``path`` with this validated state."""
        return atomic_write_json(path, self.model_dump(mode="json"))

    @classmethod
    def load(cls, path: str | Path) -> "ResearchState":
        """Load and validate a state object from JSON."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("research state payload must be an object")
        return cls.model_validate(payload)
