"""Build and freeze EvalSpec from TaskMetaData during PREPARE phase."""

from athena.evaluation.types import EvalSpec, MetricDef
from athena.data.types import DataProfile
from athena.research.models import TaskMetaData

_DEFAULT_METRICS = {
    "classification": MetricDef(
        name="f1_macro",
        direction="maximize",
        description="Macro-averaged F1 score across all classes",
    ),
    "regression": MetricDef(
        name="rmse",
        direction="minimize",
        description="Root Mean Square Error between predictions and targets",
    ),
    "binary_classification": MetricDef(
        name="roc_auc", direction="maximize", description="Area Under the ROC Curve"
    ),
}


class EvaluatorFactory:
    """Build and freeze EvalSpec from TaskMetaData and data profile."""

    @staticmethod
    def build(task: TaskMetaData, data_profile: "DataProfile") -> EvalSpec:
        """Build EvalSpec from task metadata, falling back to task_type defaults."""
        default = _DEFAULT_METRICS.get(
            task.task_type,
            MetricDef(
                name="accuracy", direction="maximize", description="Accuracy score"
            ),
        )
        # 使用任务指定的主指标，回退到基于 task_type 的默认值
        primary = MetricDef(
            name=task.primary_metric.name or default.name,
            direction=task.primary_metric.direction or default.direction,
            description=task.primary_metric.name or default.description,
        )
        secondary = [default] if default.name != primary.name else []
        return EvalSpec(primary=primary, secondary=secondary)
