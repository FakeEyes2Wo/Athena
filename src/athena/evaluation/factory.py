"""EvalSpec 默认指标：按 task_type 构造主/次指标协议。"""

from athena.evaluation.types import EvalSpec, MetricDef

_DEFAULT_METRICS = {
    "classification": MetricDef(
        name="f1_macro",
        direction="maximize",
        description="F1 score with macro averaging across classes",
    ),
    "regression": MetricDef(
        name="rmse", direction="minimize", description="Root Mean Squared Error"
    ),
    "binary_classification": MetricDef(
        name="roc_auc",
        direction="maximize",
        description="Area Under the ROC Curve",
    ),
}


def create_eval_spec(task_type: str) -> EvalSpec:
    """Create the frozen default metric protocol for one task type."""
    primary = _DEFAULT_METRICS.get(
        task_type,
        MetricDef(name="accuracy", direction="maximize", description="Accuracy"),
    )
    return EvalSpec(
        primary=MetricDef(
            name=primary.name,
            direction=primary.direction,
            description=primary.description,
        ),
        secondary=_default_secondary(task_type),
    )


def _default_secondary(task_type: str) -> list[MetricDef]:
    if task_type == "classification":
        return [
            MetricDef(name="accuracy", direction="maximize", description="Accuracy"),
            MetricDef(
                name="precision_macro",
                direction="maximize",
                description="Precision macro",
            ),
            MetricDef(
                name="recall_macro",
                direction="maximize",
                description="Recall macro",
            ),
        ]
    if task_type == "regression":
        return [
            MetricDef(
                name="mae", direction="minimize", description="Mean Absolute Error"
            ),
            MetricDef(name="r2", direction="maximize", description="R-squared"),
        ]
    if task_type == "binary_classification":
        return [
            MetricDef(name="f1", direction="maximize", description="F1 score"),
            MetricDef(name="precision", direction="maximize", description="Precision"),
            MetricDef(name="recall", direction="maximize", description="Recall"),
        ]
    return []


if __name__ == "__main__":
    spec = create_eval_spec("classification")
    print(f"create_eval_spec('classification') -> {spec.primary.name}")
