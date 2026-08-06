"""Shared primitive contracts and protocol records."""

from typing import Annotated, Literal, NewType, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, Field, StringConstraints

NonBlankText: TypeAlias = Annotated[str, StringConstraints(pattern=r"\S")]
ArtifactRef: TypeAlias = NonBlankText
CommitHash: TypeAlias = NonBlankText
ExecutionId: TypeAlias = NonBlankText

RunId = NewType("RunId", str)
HypothesisId = NewType("HypothesisId", str)
ExperimentId = NewType("ExperimentId", str)


def new_id(prefix: str) -> str:
    """生成唯一 ID，格式为 ``{prefix}_{12位随机hex}``。prefix 只能包含字母、数字和下划线。"""
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError(
            "prefix must contain at least one alphanumeric character and may otherwise "
            "contain only alphanumeric characters or underscores"
        )
    return f"{prefix}_{uuid4().hex[:12]}"


class EventEnvelope(BaseModel):
    """事件信封 — 封装事件类型、来源和载荷，附带状态版本号。"""

    kind: str
    source: str
    payload: dict[str, object]
    state_version: int = Field(ge=0)


class ErrorRecord(BaseModel):
    """错误记录 — 描述错误严重级别、错误码、消息和重试次数。"""

    severity: Literal["retry", "degrade", "fatal"]
    code: str
    message: str
    retry_count: int = Field(default=0, ge=0)
