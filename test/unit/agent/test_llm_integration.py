"""真实 LLM 装配测试：ResearchRuntime.register_defaults 需要显式 model。"""

import pandas as pd
import pytest

from athena.core.agent import settings
from athena.research.models import MetricSpec, TaskMetaData
from athena.research.runtime import ResearchRuntime


def _task() -> TaskMetaData:
    return TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="accuracy", direction="maximize"),
    )


@pytest.mark.slow
async def test_register_defaults_requires_model(tmp_path) -> None:
    """无 model → register_defaults 报错（不再静默确定性）。"""
    runtime = ResearchRuntime(project_root=tmp_path)
    with pytest.raises(RuntimeError, match="requires a model"):
        runtime.register_defaults()  # 无 model → 报错
    await runtime.aclose()


@pytest.mark.slow
async def test_register_defaults_with_model_and_workers(tmp_path) -> None:
    """带真实 model 装配六个 worker 类型；supervisor 不再是 registry 类型。"""
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.register_defaults(model=settings.model_name())  # 真实 inner builder
    assert set(runtime.kernel._registry.types) == {
        "init",
        "data",
        "plot",
        "reflection",
        "ideator",
        "code",
    }
    assert "supervisor" not in runtime.kernel._registry.types
    await runtime.aclose()


@pytest.mark.slow
async def test_task_configure_with_real_model(tmp_path) -> None:
    """真实 DeepSeek 装配下 TASK_CONFIGURE 提交 task 事实 → PREPARE。"""
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.register_defaults(model=settings.model_name())  # 真实 inner builder
    data = tmp_path / "d.csv"
    pd.DataFrame({"age": range(20), "income": range(20), "label": [0, 1] * 10}).to_csv(
        data, index=False
    )
    result = await runtime.dispatch(
        "TASK_CONFIGURE",
        {
            "task_type": "classification",
            "data_type": "tabular",
            "target_vars": ["label"],
            "primary_metric": "accuracy",
            "direction": "maximize",
            "data_path": str(data),
            "target": "label",
        },
    )
    assert result["configured"] is True
    assert result["phase"] == "PREPARE"
    assert runtime.state.facts().task_ref is not None
    await runtime.aclose()
