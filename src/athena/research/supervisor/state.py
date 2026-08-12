"""Atomic persistence for the autonomous Supervisor's ``state.json``."""

import json
import os
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from athena.research.supervisor.plans import PlanState


class ResearchState(BaseModel):
    """Small durable checkpoint for unfinished autonomous research."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["RUNNING", "WAITING", "COMPLETED", "STOPPED"]
    phase: Literal["PREPARE", "SEARCH", "VALIDATE", "COMPLETED"]
    search_limit: int = Field(ge=0)
    concurrency: int = Field(ge=1)
    # SEARCH 调度模式：False=自动按优先级出队；True=每个假设生成后等待人工选定。
    manual_mode: bool = False
    plans: dict[str, PlanState] = Field(default_factory=dict)
    validation: dict[str, object] | None = None
    # PREPARE 产出的 EDA 工作区目录（Ideator 自行探索）；仅路径元数据，非 EDA 结果。
    eda_dir: str | None = None

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
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                payload = self.model_dump(mode="json")
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return target

    @classmethod
    def load(cls, path: str | Path) -> "ResearchState":
        """Load and validate a state object from JSON."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("research state payload must be an object")
        return cls.model_validate(payload)
