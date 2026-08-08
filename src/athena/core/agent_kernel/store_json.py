"""AgentGraphStore 快照与 journal 的 JSON 序列化（生产持久化基础，设计 §10）。

把 ``snapshot()`` 与 ``journal`` 编码为可读 JSON，替代 pickle 骨架；异常实例
（``CommandResult.error``）为进程内重试语义，跨进程恢复时丢弃。配合
``AgentGraphStore.load()`` 重放即可重建 Kernel 状态。
"""

import json
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from athena.core.agent_kernel.store import (
    AgentGraphStore,
    AgentRecord,
    CommandResult,
    JournalRecord,
    OutboxRecord,
    RunRecord,
    StoreSnapshot,
    WaitRecord,
)
from athena.core.agent_kernel.types import (
    AgentEvent,
    AgentMessage,
    AgentStatus,
    RunStatus,
    RunSummary,
)

_DATACLASSES = {
    "AgentRecord": AgentRecord,
    "CommandResult": CommandResult,
    "JournalRecord": JournalRecord,
    "OutboxRecord": OutboxRecord,
    "RunRecord": RunRecord,
    "StoreSnapshot": StoreSnapshot,
    "WaitRecord": WaitRecord,
    "AgentEvent": AgentEvent,
    "AgentMessage": AgentMessage,
    "RunSummary": RunSummary,
}
_ENUMS = {"AgentStatus": AgentStatus, "RunStatus": RunStatus}


def _encode(value: Any) -> Any:
    if isinstance(value, BaseException):
        return {"__k__": "exc", "t": type(value).__name__}
    if isinstance(value, Enum):
        return {"__k__": "enum", "n": type(value).__name__, "v": value.value}
    if isinstance(value, tuple):
        return {"__k__": "tuple", "d": [_encode(v) for v in value]}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__k__": "dc",
            "n": type(value).__name__,
            "d": {f.name: _encode(getattr(value, f.name)) for f in fields(value)},
        }
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and "__k__" in value:
        kind = value["__k__"]
        if kind == "exc":
            return None  # 异常实例不跨进程恢复
        if kind == "enum":
            return _ENUMS[value["n"]](value["v"])
        if kind == "tuple":
            return tuple(_decode(v) for v in value["d"])
        if kind == "dc":
            cls = _DATACLASSES[value["n"]]
            return cls(**{f: _decode(v) for f, v in value["d"].items()})
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def store_to_json(store: AgentGraphStore) -> str:
    """把 store 快照与 journal 序列化为 JSON。"""
    return json.dumps(
        {
            "snapshot": _encode(store.snapshot()),
            "journal": [_encode(r) for r in store.journal],
        },
        ensure_ascii=False,
    )


def load_store_json(text: str, store: AgentGraphStore) -> None:
    """从 JSON 重建 store（快照 + journal 重放）。"""
    data = json.loads(text)
    snapshot = _decode(data["snapshot"])
    journal = [_decode(r) for r in data["journal"]]
    store.load(snapshot, journal)
