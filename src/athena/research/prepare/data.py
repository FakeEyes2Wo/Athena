"""Platform-owned CSV preparation and prompt contracts."""

import csv
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
            "it: VALIDATE changes the variable to the held-out split."
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
            "prediction_id_column, prediction_column, label_column naming the "
            "target in labels.csv, optional probability_columns, "
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
            "do not hardcode it because VALIDATE changes the variable."
        )

    def prompt_block(self) -> str:
        """Wrap the durable contract for a Supervisor prompt."""
        return data_contract_block(self.contract_text())


async def prepare_platform_split(runtime: Any) -> DataContract | None:
    """Materialize a deterministic CSV split and persist its agent contract."""
    dataset = runtime.config.research.dataset
    if dataset.path is None or dataset.target_column is None:
        # GUI selects an existing split directory rather than a source CSV.
        # Recognize it without opening labels or FINAL files or repartitioning.
        state = getattr(runtime, "state", None)
        data_root = getattr(state, "data_root", None)
        if not data_root:
            return None
        split_dir = Path(data_root).resolve()
        train = split_dir / "train.csv"
        search = split_dir / "search_features.csv"
        if not train.is_file() or not search.is_file():
            return None
        _validate_visible_split(train, "training", target_column=dataset.target_column)
        _validate_visible_split(
            search,
            "SEARCH feature",
            target_column=dataset.target_column,
            forbid_target=True,
            validate_optional_ids=True,
        )
        contract = DataContract(
            train_csv=train,
            predict_features_csv=search,
            dataset_path=split_dir,
            group_column=dataset.group_column,
        )
        runtime.state.data_contract = contract.contract_text()
        runtime.state.save(runtime.state_path)
        await runtime.publish_output(
            source="supervisor",
            channel="text",
            text="PREPARE: using existing train/SEARCH partition; no re-splitting.",
        )
        return contract

    # Materialize the exact train/search/final filenames consumed downstream.
    split_dir = runtime.workspaces_root / "data_split"
    materialize_csv_split(
        dataset.path,
        split_dir,
        dataset.target_column,
        SplitSpec(
            search_frac=0.2,
            final_frac=0.2,
            seed=dataset.split_seed,
            group_column=dataset.group_column,
        ),
    )
    contract = DataContract(
        train_csv=split_dir / "train.csv",
        predict_features_csv=split_dir / "search_features.csv",
        dataset_path=dataset.path,
        group_column=dataset.group_column,
    )

    # Persist before agents run because later SEARCH turns do not receive task text.
    runtime.state.data_contract = contract.contract_text()
    runtime.state.save(runtime.state_path)
    grouping = f" grouped by {dataset.group_column}" if dataset.group_column else ""
    await runtime.publish_output(
        source="supervisor",
        channel="text",
        text=f"PREPARE: platform data split ready at {split_dir.resolve()}{grouping}.",
    )
    return contract


def _validate_visible_split(
    path: Path,
    description: str,
    *,
    target_column: str | None = None,
    forbid_target: bool = False,
    validate_optional_ids: bool = False,
) -> None:
    """Validate visible split semantics without opening labels or FINAL."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if not header or not any(value.strip() for value in header):
                raise ValueError(
                    f"existing split {description} file {path} has no CSV header; "
                    "provide a valid train.csv or search_features.csv before PREPARE"
                )
            normalized = [value.strip() for value in header]
            if any(not value for value in normalized) or len(set(normalized)) != len(
                normalized
            ):
                raise ValueError(
                    f"existing split {description} file {path} has empty or duplicate "
                    "column names; repair its CSV header before PREPARE"
                )
            width = len(header)
            target_index = None
            if target_column is not None:
                if target_column in normalized:
                    if forbid_target:
                        raise ValueError(
                            f"existing split {description} file {path} contains "
                            f"target column {target_column!r}; SEARCH features must "
                            "not expose labels before scoring"
                        )
                    target_index = normalized.index(target_column)
                elif not forbid_target:
                    raise ValueError(
                        f"existing split {description} file {path} is missing "
                        f"configured target column {target_column!r}"
                    )
            id_index = (
                normalized.index("__athena_row_id")
                if validate_optional_ids and "__athena_row_id" in normalized
                else None
            )
            seen_ids: set[str] = set()
            rows = 0
            for row_number, values in enumerate(reader, start=2):
                if len(values) != width:
                    raise ValueError(
                        f"existing split {description} file {path} row {row_number} "
                        f"has {len(values)} values; expected {width}"
                    )
                if target_index is not None and not values[target_index].strip():
                    raise ValueError(
                        f"existing split {description} file {path} row {row_number} "
                        f"has an empty configured target {target_column!r}"
                    )
                if id_index is not None:
                    row_id = values[id_index].strip()
                    if not row_id:
                        raise ValueError(
                            f"existing split {description} file {path} row "
                            f"{row_number} has an empty '__athena_row_id'"
                        )
                    if row_id in seen_ids:
                        raise ValueError(
                            f"existing split {description} file {path} has duplicate "
                            f"'__athena_row_id' {row_id!r}"
                        )
                    seen_ids.add(row_id)
                rows += 1
    except UnicodeDecodeError as exc:
        # Trigger: the dataset CSV header or records contain invalid UTF-8 bytes.
        raise ValueError(
            f"existing split {description} file {path} is not UTF-8 CSV; "
            "export it as UTF-8 before PREPARE"
        ) from exc
    if rows == 0:
        raise ValueError(
            f"existing split {description} file {path} contains no data rows; "
            "provide usable training and SEARCH rows before PREPARE"
        )
