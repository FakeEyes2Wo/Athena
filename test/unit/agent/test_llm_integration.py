import json

import pandas as pd
import pytest

from athena.core.agent import settings
from athena.research.models import MetricSpec, TaskMetaData
from athena.research.project_runtime import ProjectRuntime


def _task() -> TaskMetaData:
    return TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="accuracy", direction="maximize"),
    )


@pytest.mark.slow
async def test_prepare_data_analysis_with_real_llm(tmp_path) -> None:
    """真实 DeepSeek 跑通 PREPARE：init 产出 eval.py + data 提交 EDA bundle。"""
    project = ProjectRuntime(tmp_path)
    project.register_defaults(model=settings.model_name())  # 真实 inner builder
    await project.open()
    data = tmp_path / "d.csv"
    pd.DataFrame({"age": range(20), "income": range(20), "label": [0, 1] * 10}).to_csv(
        data, index=False
    )
    await project.configure(_task())
    accepted = await project.prepare_data_analysis(str(data), "label")
    assert accepted.startswith("sha256:")
    await project.close()
