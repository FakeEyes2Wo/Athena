import pytest
from athena.core.schemas import TaskMetaData, MetricSpec
from athena.workflows.prepare.data_analysis import DataProfile, DataTools, ProcessingLog
from athena.workflows.prepare.evaluator_factory import EvaluatorFactory


def test_evaluator_factory_classification():
    task = TaskMetaData(
        task_type="classification",
        data_type="tabular",
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )
    profile = DataProfile(row_count=1000, col_count=10, task_type_hint="classification")
    spec = EvaluatorFactory.build(task, profile)
    assert spec.primary.name == "f1_macro"
    assert spec.primary.direction == "maximize"


def test_data_tools_sample():
    import tempfile, os
    import pandas as pd

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.csv")
        pd.DataFrame({"a": range(100), "b": range(100)}).to_csv(path, index=False)
        tools = DataTools(path)
        ref = tools.sample(seed=42, n=10)
        sampled_path = ref.split("://", 1)[1]
        df = pd.read_csv(sampled_path)
        assert len(df) == 10
        assert list(df.columns) == ["a", "b"]
