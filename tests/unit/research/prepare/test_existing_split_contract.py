from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from athena.research.config import DatasetConfig
from athena.research.prepare.data import prepare_platform_split


@pytest.mark.asyncio
async def test_existing_gui_split_is_reused_without_opening_data(tmp_path, monkeypatch):
    for name in ("train.csv", "search_features.csv"):
        (tmp_path / name).touch()
    runtime = SimpleNamespace(
        config=SimpleNamespace(research=SimpleNamespace(dataset=DatasetConfig())),
        state=SimpleNamespace(data_root=str(tmp_path), save=Mock()),
        state_path=tmp_path / "state.json",
        publish_output=AsyncMock(),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("contract discovery must not read dataset contents")

    monkeypatch.setattr(Path, "open", forbidden)
    contract = await prepare_platform_split(runtime)
    assert contract.train_csv == tmp_path / "train.csv"
    assert contract.predict_features_csv == tmp_path / "search_features.csv"
    assert "Train ONLY" in runtime.state.data_contract
    runtime.state.save.assert_called_once()
