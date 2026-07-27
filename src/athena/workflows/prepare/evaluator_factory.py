from athena.core.schemas import TaskMetaData, MetricDef, EvalSpec

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
    @staticmethod
    def build(task: TaskMetaData, data_profile: "DataProfile") -> EvalSpec:
        default = _DEFAULT_METRICS.get(
            task.task_type,
            MetricDef(
                name="accuracy", direction="maximize", description="Accuracy score"
            ),
        )
        # Use task's specified primary, fallback to default based on task_type
        primary = MetricDef(
            name=task.primary_metric.name or default.name,
            direction=task.primary_metric.direction or default.direction,
            description=task.primary_metric.name or default.description,
        )
        secondary = [default] if default.name != primary.name else []
        return EvalSpec(primary=primary, secondary=secondary)

    @staticmethod
    def freeze(spec: EvalSpec) -> EvalSpec:
        """Returns the spec — immutability is enforced by convention (don't mutate after freeze)."""
        return spec
