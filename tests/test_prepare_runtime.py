import json
from pathlib import Path

import pandas as pd
import pytest

from athena.research.models import MetricSpec, TaskMetaData
from athena.workflows.prepare.runtime import prepare_workflow_data


def _task() -> TaskMetaData:
    return TaskMetaData(
        task_type="classification",
        data_type="tabular",
        target_vars=["label"],
        primary_metric=MetricSpec(name="f1_macro", direction="maximize"),
    )


@pytest.mark.asyncio
async def test_prepare_workflow_data_preserves_raw_and_freezes_splits(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    frame = pd.DataFrame(
        {
            "number": [1.0, None, 100.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
            "category": ["a", None, "b", "a", "b", "a", "b", "a", "b", "a"],
            "label": [0, 1, 0, 1, 0, 1, 0, 1, 0, 1],
        }
    )
    frame.to_csv(source, index=False)
    original = source.read_bytes()

    prepared = await prepare_workflow_data(
        source,
        target="label",
        task=_task(),
        output_dir=tmp_path / "run",
        seed=42,
        validation_ratio=0.2,
        test_ratio=0.2,
    )

    assert source.read_bytes() == original
    assert (
        Path(prepared.processing_log.raw_copy.removeprefix("artifact://")).read_bytes()
        == original
    )
    assert prepared.profile.row_count == 10
    assert prepared.profile.target_col == "label"
    assert prepared.eval_spec.eval_script
    manifest = json.loads(prepared.split_manifest_path.read_text(encoding="utf-8"))
    assert manifest["target"] == "label"
    assert set(manifest["splits"]) == {"train", "validation", "test"}
    partitions = {
        name: pd.read_csv(Path(ref.removeprefix("artifact://")))
        for name, ref in manifest["splits"].items()
    }
    assert {name: len(part) for name, part in partitions.items()} == {
        "train": 6,
        "validation": 2,
        "test": 2,
    }
    assert all(not part.isna().any().any() for part in partitions.values())
    assert (
        len(
            {
                Path(ref.removeprefix("artifact://")).resolve()
                for ref in manifest["splits"].values()
            }
        )
        == 3
    )


@pytest.mark.asyncio
async def test_prepare_workflow_data_rejects_missing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    pd.DataFrame({"feature": [1, 2]}).to_csv(source, index=False)

    with pytest.raises(ValueError, match="target column"):
        await prepare_workflow_data(
            source,
            target="label",
            task=_task(),
            output_dir=tmp_path / "run",
        )
