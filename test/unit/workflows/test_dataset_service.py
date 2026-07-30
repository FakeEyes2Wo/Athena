"""Tests for dataset_service.create_data_card."""

import json
import tempfile
from pathlib import Path

import pytest

from athena.storage.artifact_store import LocalArtifactStore
from athena.workflows.prepare.dataset_service import create_data_card


@pytest.fixture
def temp_store():
    with tempfile.TemporaryDirectory() as tmp:
        yield LocalArtifactStore(Path(tmp) / "artifacts")


@pytest.fixture
def sample_csv():
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "train.csv"
        csv_path.write_text(
            "PassengerId,Survived,Pclass,Name\n"
            "1,0,3,Smith\n2,1,1,Jones\n3,1,3,Brown\n"
        )
        yield csv_path


@pytest.mark.asyncio
async def test_create_data_card_fingerprint(temp_store, sample_csv):
    card = await create_data_card(str(sample_csv), temp_store)
    assert card.dataset_ref.startswith("sha256:")
    assert len(card.fingerprint) == 64
    assert card.schema_ref.startswith("sha256:")
    assert card.split_manifest_ref is None


@pytest.mark.asyncio
async def test_create_data_card_idempotent(temp_store, sample_csv):
    card1 = await create_data_card(str(sample_csv), temp_store)
    card2 = await create_data_card(str(sample_csv), temp_store)
    assert card1.dataset_ref == card2.dataset_ref
    assert card1.fingerprint == card2.fingerprint
    assert card1.schema_ref == card2.schema_ref


@pytest.mark.asyncio
async def test_create_data_card_schema(temp_store, sample_csv):
    card = await create_data_card(str(sample_csv), temp_store)
    schema_text = await temp_store.get_text(card.schema_ref)
    schema = json.loads(schema_text)
    assert "PassengerId" in schema["columns"]
    assert "Survived" in schema["columns"]
    assert schema["row_count"] == 3
    assert schema["column_count"] == 4
