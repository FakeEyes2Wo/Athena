"""ResearchRuntime 翻译层测试（dynamic-orchestration §8 条件 7）。

ResearchRuntime 不再拥有第二套生命周期；dispatch 委托 ProjectRuntime。
"""

from pathlib import Path

import pandas as pd
import pytest

from athena.research import ResearchMethod, ResearchRuntime
from athena.research.project_runtime import ProjectRuntime
from athena.research.models import MetricSpec, TaskMetaData
from test.unit._support import make_project


def _dataset(tmp_path: Path) -> Path:
    """写一个小型 CSV，供 DataAgent 脚本分析。"""
    path = tmp_path / "dataset.csv"
    pd.DataFrame({"age": range(20), "label": [0, 1] * 10}).to_csv(path, index=False)
    return path


def _task() -> TaskMetaData:
    return TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="accuracy", direction="maximize"),
    )


def _task_params() -> dict[str, object]:
    return {
        "task_type": "classification",
        "data_type": "tabular",
        "target_vars": ["label"],
        "primary_metric": "f1_macro",
        "direction": "maximize",
    }


async def _make_runtime(tmp_path) -> tuple[ResearchRuntime, ProjectRuntime]:
    project = make_project(tmp_path)
    await project.open()
    return ResearchRuntime(project=project), project


@pytest.mark.asyncio
async def test_runtime_delegates_phase_to_project(tmp_path) -> None:
    """§8：phase 由 ProjectRuntime 投影，不保存在 ResearchRuntime。"""
    runtime, project = await _make_runtime(tmp_path)
    assert runtime.phase == project.projected_phase()
    await project.configure(_task())
    assert runtime.phase == "CONFIGURED"
    await project.close()


@pytest.mark.asyncio
async def test_runtime_configures_project(tmp_path) -> None:
    """TASK_CONFIGURE 翻译为 ProjectRuntime.configure。"""
    runtime, project = await _make_runtime(tmp_path)
    result = await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    assert result["configured"] is True
    assert project.phase == "CONFIGURED"
    with pytest.raises(ValueError, match="unknown method"):
        await runtime.dispatch("unknown_research_method", {})
    await project.close()


@pytest.mark.asyncio
async def test_runtime_search_validate_report_delegate(tmp_path) -> None:
    """SEARCH/VALIDATE/REPORT 翻译为 ProjectRuntime 阶段方法。"""
    runtime, project = await _make_runtime(tmp_path)
    await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    started = await runtime.dispatch(
        ResearchMethod.SEARCH_START, {"hypothesis": "假设A"}
    )
    assert started["status"] == "started"
    assert project.phase == "SEARCH"
    validation = await runtime.dispatch(
        ResearchMethod.VALIDATE_START, {"experiment_ref": "sota"}
    )
    assert "validation_ref" in validation
    report = await runtime.dispatch(
        ResearchMethod.REPORT_GENERATE, {"report_text": "最终报告正文"}
    )
    assert "report_ref" in report
    assert project.phase == "COMPLETED"
    await project.close()


@pytest.mark.asyncio
async def test_runtime_pause_resume_stop_delegate(tmp_path) -> None:
    """SEARCH_PAUSE/RESUME/STOP 翻译为 ProjectRuntime 控制方法。"""
    runtime, project = await _make_runtime(tmp_path)
    await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, _task_params())
    await project.prepare_data_analysis(str(_dataset(tmp_path)), "label")
    await runtime.dispatch(ResearchMethod.SEARCH_START, {"hypothesis": "假设A"})
    paused = await runtime.dispatch(ResearchMethod.SEARCH_PAUSE, {})
    assert paused["status"] == "paused"
    assert project.project_status() == "PAUSED"
    resumed = await runtime.dispatch(ResearchMethod.SEARCH_RESUME, {})
    assert resumed["status"] == "running"
    stopped = await runtime.dispatch(ResearchMethod.SEARCH_STOP, {})
    assert stopped["status"] == "stopped"
    assert project.project_status() == "CANCELLED"
    await project.close()


@pytest.mark.asyncio
async def test_runtime_requires_project(tmp_path) -> None:
    """无 ProjectRuntime 时 dispatch 阶段方法报错。"""
    runtime = ResearchRuntime()
    with pytest.raises(RuntimeError, match="requires a ProjectRuntime"):
        await runtime.dispatch(ResearchMethod.TASK_CONFIGURE, {})
