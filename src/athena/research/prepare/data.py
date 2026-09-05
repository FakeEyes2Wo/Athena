"""Platform-owned CSV preparation and prompt contracts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athena.research.splitter import SplitSpec, materialize_csv_split
from athena.research.supervisor.prompt_context import data_contract_block


@dataclass(frozen=True)
class DataContract:
    """Render one platform data contract for every research agent role."""

    train_csv: Path
    predict_features_csv: Path
    dataset_path: Path
    group_column: str | None = None

    @property
    def grouping(self) -> str:
        """Describe the optional group isolation rule."""
        if self.group_column is None:
            return ""
        return (
            f"Rows were kept together by {self.group_column!r}, so no group "
            "spans two splits."
        )

    @property
    def moving_target(self) -> str:
        """The rule candidates break by pairing the predict file with a fixed one.

        Reading ``ATHENA_PREDICT_FEATURES`` is necessary but not sufficient. On
        2026-08-31 a candidate did read it for its features and then scored those
        predictions against a hardcoded ``search_labels.csv``; under VALIDATE the
        features became the held-out split and the labels did not, so it died on
        ``inconsistent numbers of samples: [169725, 169965]`` and took a
        five-hour run with it. Tuning on the search split is legitimate -- what
        is not is assuming the rows you are asked to predict *are* that split.
        """
        return (
            "That variable is the only thing that moves between SEARCH and "
            "VALIDATE. Every other path you read stays exactly where it is, so "
            "never pair the two: do not score, index, align, or concatenate "
            "predictions made from ATHENA_PREDICT_FEATURES against any fixed "
            "label file, row count, or saved index. If you want a metric or a "
            "decision threshold from the search split, load that split's "
            "features under their own name and predict them separately -- the "
            "row counts differ, and a script that conflates them raises "
            "'inconsistent numbers of samples' the moment VALIDATE re-runs it."
        )

    def candidate_task(self, task: str) -> str:
        """Add train and prediction constraints to a candidate task."""
        return (
            f"{task}\n\nThe platform owns the data split. Train ONLY on "
            f"{self.train_csv.resolve()}. {self.grouping}\n"
            f"Do NOT read {self.dataset_path} for training, and do NOT use any "
            "other split of it. Fitting on scored rows invalidates the metric.\n"
            f"{self.predict_features_csv.resolve()} holds exactly the rows to "
            "predict, with labels withheld. Read that path from the environment "
            "variable ATHENA_PREDICT_FEATURES (os.environ), rather than hardcoding "
            "it: VALIDATE changes the variable to the held-out split.\n"
            f"{self.moving_target}"
        )

    def evaluator_task(self, task: str) -> str:
        """Add the platform split rules to an evaluator task."""
        return (
            f"{task}\n\nThe platform has already split the dataset under "
            f"{self.train_csv.parent.resolve()}. {self.grouping}\n"
            "Do NOT create another split. Build the complete evaluator under "
            "evaluate/ with metric.json, eval_metrics.py, labels.csv (or labels/), "
            "HANDOFF.md, and pyproject.toml. Declare contract_version=2, task_id, "
            "task_type, primary_metric, the complete class_labels for classification, "
            "prediction_file=predictions__{task_id}.csv, "
            "prediction_id_column, prediction_column, optional probability_columns, "
            "metrics_file=metrics_public_test.csv, and eval_script in metric.json. "
            "Use the actual dataset identity/target columns rather than assuming "
            "a fixed framework class list. SEARCH and FINAL must use the same complete "
            "class_labels; macro-F1 must not drop classes absent from one split. Keep "
            "final labels hidden from SEARCH."
        )

    def contract_text(self) -> str:
        """Render the durable contract stored in research state."""
        return (
            f"Train ONLY on {self.train_csv.resolve()}. {self.grouping}\n"
            f"Do NOT read {self.dataset_path} for training, and do NOT use any "
            "other split of it. Fitting on scored rows invalidates the metric.\n"
            "Predict exactly the rows in the CSV named by the environment "
            "variable ATHENA_PREDICT_FEATURES; during SEARCH that is "
            f"{self.predict_features_csv.resolve()}. Read it from os.environ and "
            "do not hardcode it because VALIDATE changes the variable.\n"
            f"{self.moving_target}"
        )

    def prompt_block(self) -> str:
        """Wrap the durable contract for a Supervisor prompt."""
        return data_contract_block(self.contract_text())


def _contract(runtime: Any, split_dir: Path) -> DataContract:
    """Build the durable contract for an already materialized split."""
    return DataContract(
        train_csv=split_dir / "train.csv",
        predict_features_csv=split_dir / "search_features.csv",
        dataset_path=runtime.config.dataset_path,
        group_column=runtime.config.group_column,
    )


async def prepare_platform_split(runtime: Any) -> DataContract | None:
    """Materialize a deterministic CSV split and persist its agent contract."""
    config = runtime.config
    if config.dataset_path is None or config.target_column is None:
        return None

    # Materialize the exact train/search/final filenames consumed downstream.
    split_dir = runtime.workspaces_root / "data_split"
    materialize_csv_split(
        config.dataset_path,
        split_dir,
        config.target_column,
        SplitSpec(
            search_frac=0.2,
            final_frac=0.2,
            seed=config.split_seed,
            group_column=config.group_column,
        ),
    )
    contract = _contract(runtime, split_dir)

    # Persist before agents run because later SEARCH turns do not receive task text.
    runtime.state.data_contract = contract.contract_text()
    runtime.state.save(runtime.state_path)
    grouping = f" grouped by {config.group_column}" if config.group_column else ""
    await runtime.publish_output(
        source="supervisor",
        channel="text",
        text=f"PREPARE: platform data split ready at {split_dir.resolve()}{grouping}.",
    )
    return contract
