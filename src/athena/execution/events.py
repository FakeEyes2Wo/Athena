"""Source-independent contracts for observing long-running executions."""

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType

JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


class ExecutionEventKind(StrEnum):
    """Source-independent execution lifecycle event kinds."""

    STARTED = "started"
    ACTIVITY = "activity"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionState(StrEnum):
    """Health and terminal states derived by the monitor."""

    RUNNING = "RUNNING"
    STALLED = "STALLED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class MonitorLimits:
    """Time limits applied to one execution, expressed in seconds."""

    stalled_after: float = 300.0
    timeout_after: float = 3600.0

    def __post_init__(self) -> None:
        _require_positive_finite(self.stalled_after, "stalled_after")
        _require_positive_finite(self.timeout_after, "timeout_after")


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """An execution event normalized by its source adapter."""

    execution_id: str
    kind: ExecutionEventKind
    occurred_at: datetime = field(default_factory=utc_now)
    advances_progress: bool = False
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise ValueError("execution_id must be a non-empty string")
        if not isinstance(self.kind, ExecutionEventKind):
            raise TypeError("kind must be an ExecutionEventKind")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("occurred_at must be an aware UTC timestamp")
        if not isinstance(self.advances_progress, bool):
            raise TypeError("advances_progress must be a bool")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        metadata: dict[str, JsonValue] = {}
        for raw_key, value in self.metadata.items():
            key = _require_string_key(raw_key)
            metadata[key] = _copy_json_value(value, f"metadata.{key}")
        object.__setattr__(self, "metadata", MappingProxyType(metadata))


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot:
    """Read-only view of one execution's observed state."""

    execution_id: str
    state: ExecutionState
    started_at: datetime
    last_event_at: datetime
    last_progress_at: datetime
    limits: MonitorLimits
    stalled_at: datetime | None = None
    timeout_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HealthStateEvent:
    """A derived state transition emitted by the monitor."""

    execution_id: str
    state: ExecutionState
    changed_at: datetime
    snapshot: ExecutionSnapshot


HealthEventSink = Callable[[HealthStateEvent], Awaitable[None]]


def _require_positive_finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")


def _require_string_key(key: object) -> str:
    if not isinstance(key, str):
        raise TypeError("metadata must contain only JSON-safe string keys")
    return key


def _copy_json_value(value: object, path: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} must contain only JSON-safe values")
        return value
    if isinstance(value, list):
        return [_copy_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, dict):
        copied: dict[str, JsonValue] = {}
        for raw_key, item in value.items():
            key = _require_string_key(raw_key)
            copied[key] = _copy_json_value(item, f"{path}.{key}")
        return copied
    raise TypeError(f"{path} must contain only JSON-safe values")
