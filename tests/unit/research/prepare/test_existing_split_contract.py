from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from athena.research.config import DatasetConfig
from athena.research.prepare.data import prepare_platform_split


@pytest.mark.asyncio
async def test_existing_gui_split_is_reused_without_opening_data(tmp_path, monkeypatch):
    (tmp_path / "train.csv").write_text("feature,target\n1,a\n", encoding="utf-8")
    (tmp_path / "search_features.csv").write_text("feature\n2\n", encoding="utf-8")
    (tmp_path / "search_labels.csv").write_text("label\nprivate\n", encoding="utf-8")
    (tmp_path / "final_labels.csv").write_text("label\nsecret\n", encoding="utf-8")
    runtime = SimpleNamespace(
        config=SimpleNamespace(research=SimpleNamespace(dataset=DatasetConfig())),
        state=SimpleNamespace(data_root=str(tmp_path), save=Mock()),
        state_path=tmp_path / "state.json",
        publish_output=AsyncMock(),
    )

    original_open = Path.open
    unreadable = {
        tmp_path / "search_labels.csv",
        tmp_path / "final_labels.csv",
    }

    def guard_labels(path, *args, **kwargs):
        if path in unreadable:
            raise AssertionError("existing-split validation must not read labels")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard_labels)
    contract = await prepare_platform_split(runtime)
    assert contract.train_csv == tmp_path / "train.csv"
    assert contract.predict_features_csv == tmp_path / "search_features.csv"
    assert "Train ONLY" in runtime.state.data_contract
    runtime.state.save.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,contents",
    [
        ("train.csv", "feature,target\n"),
        ("search_features.csv", "feature\n"),
    ],
)
async def test_existing_gui_split_rejects_empty_visible_partition(
    tmp_path, filename, contents
):
    (tmp_path / "train.csv").write_text("feature,target\n1,a\n", encoding="utf-8")
    (tmp_path / "search_features.csv").write_text("feature\n2\n", encoding="utf-8")
    (tmp_path / filename).write_text(contents, encoding="utf-8")
    runtime = SimpleNamespace(
        config=SimpleNamespace(research=SimpleNamespace(dataset=DatasetConfig())),
        state=SimpleNamespace(data_root=str(tmp_path), save=Mock()),
        state_path=tmp_path / "state.json",
        publish_output=AsyncMock(),
    )

    with pytest.raises(ValueError, match="no data rows"):
        await prepare_platform_split(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "train_csv",
    ["feature\n1\n", "feature,target\n1,\n"],
)
async def test_existing_split_validates_configured_training_target(tmp_path, train_csv):
    (tmp_path / "train.csv").write_text(train_csv, encoding="utf-8")
    (tmp_path / "search_features.csv").write_text("feature\n2\n", encoding="utf-8")
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            research=SimpleNamespace(dataset=DatasetConfig(target_column="target"))
        ),
        state=SimpleNamespace(data_root=str(tmp_path), save=Mock()),
        state_path=tmp_path / "state.json",
        publish_output=AsyncMock(),
    )

    with pytest.raises(ValueError, match="target"):
        await prepare_platform_split(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "search_csv",
    [
        "feature,target\n2,private\n",
        "__athena_row_id,feature\n,2\n",
        "__athena_row_id,feature\nrow-a,2\nrow-a,3\n",
    ],
)
async def test_existing_split_rejects_search_labels_and_invalid_declared_ids(
    tmp_path, search_csv
):
    (tmp_path / "train.csv").write_text("feature,target\n1,public\n", encoding="utf-8")
    (tmp_path / "search_features.csv").write_text(search_csv, encoding="utf-8")
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            research=SimpleNamespace(dataset=DatasetConfig(target_column="target"))
        ),
        state=SimpleNamespace(data_root=str(tmp_path), save=Mock()),
        state_path=tmp_path / "state.json",
        publish_output=AsyncMock(),
    )

    with pytest.raises(ValueError, match="target|__athena_row_id"):
        await prepare_platform_split(runtime)


@pytest.mark.asyncio
async def test_existing_split_allows_platform_generated_search_ids(tmp_path):
    (tmp_path / "train.csv").write_text("feature,target\n1,public\n", encoding="utf-8")
    # Preserve the existing legacy split path when it has no explicit row IDs.
    (tmp_path / "search_features.csv").write_text("feature\n2\n", encoding="utf-8")
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            research=SimpleNamespace(dataset=DatasetConfig(target_column="target"))
        ),
        state=SimpleNamespace(data_root=str(tmp_path), save=Mock()),
        state_path=tmp_path / "state.json",
        publish_output=AsyncMock(),
    )

    contract = await prepare_platform_split(runtime)

    assert contract is not None
    assert contract.predict_features_csv == tmp_path / "search_features.csv"
