"""Supervisor Kaggle 接入门控的单元测试。"""

import asyncio

from athena.agents.supervisor_agent import supervisor_tool_registry
from athena.core.tool_types import ToolContext


class _Actions:
    """只实现 supervisor 门控相关动作的最小 fake。"""

    def __init__(self) -> None:
        self.enabled = None
        self.download = None
        self.understanding = None

    async def set_kaggle_enabled(self, enabled: bool, download: bool = True) -> dict:
        self.enabled = enabled
        self.download = download
        return {"kaggle_enabled": enabled, "download": download}

    async def record_task_understanding(self, **payload) -> dict:
        self.understanding = dict(payload)
        return {"recorded": True, "task_understanding": self.understanding}

    async def read_hypotheses(self) -> dict:
        return {"pending": [], "sota": None, "attempts": 0, "search_limit": 10}


async def _noop(kind: str, ref: str, data=None) -> None:
    del kind, ref, data


async def test_configure_kaggle_tool_forwards_decision() -> None:
    actions = _Actions()
    registry = supervisor_tool_registry(actions)
    names = {item.name for item in registry.specs}
    assert "configure_kaggle" in names
    tool = registry.resolve("configure_kaggle")
    ctx = ToolContext("configure_kaggle", "c1", _noop, asyncio.Event())
    result = await tool.execute({"enabled": True, "download": False}, ctx)
    assert result["kaggle_enabled"] is True
    assert result["download"] is False
    assert actions.enabled is True
    assert actions.download is False


async def test_read_hypotheses_tool_exists_and_forwards() -> None:
    actions = _Actions()
    registry = supervisor_tool_registry(actions)
    tool = registry.resolve("read_hypotheses")
    ctx = ToolContext("read_hypotheses", "c3", _noop, asyncio.Event())
    result = await tool.execute({}, ctx)
    assert result["search_limit"] == 10


async def test_configure_kaggle_tool_rejects_missing_enabled() -> None:
    actions = _Actions()
    tool = supervisor_tool_registry(actions).resolve("configure_kaggle")
    ctx = ToolContext("configure_kaggle", "c2", _noop, asyncio.Event())
    try:
        await tool.execute({}, ctx)
    except Exception:
        # pydantic 校验拒绝缺 enabled 的请求
        pass
    else:
        raise AssertionError("expected validation error for missing enabled")


async def test_record_task_understanding_tool_forwards_structured_fields() -> None:
    actions = _Actions()
    registry = supervisor_tool_registry(actions)
    names = {item.name for item in registry.specs}
    assert "record_task_understanding" in names
    tool = registry.resolve("record_task_understanding")
    ctx = ToolContext("record_task_understanding", "c3", _noop, asyncio.Event())
    result = await tool.execute(
        {
            "title": "Titanic 分类",
            "dataset": "train.csv",
            "target": "survived",
            "task_type": "classification",
            "primary_metric": "accuracy",
            "direction": "maximize",
            "metric_source": "human",
            "human_primary_metric": "accuracy",
            "human_direction": "maximize",
            "evaluation_plan": "holdout accuracy",
        },
        ctx,
    )
    assert result["recorded"] is True
    assert actions.understanding["task_type"] == "classification"
    assert actions.understanding["primary_metric"] == "accuracy"
    assert actions.understanding["human_primary_metric"] == "accuracy"
