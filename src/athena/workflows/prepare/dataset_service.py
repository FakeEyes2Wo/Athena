"""Dataset ingestion service: immutable copy, fingerprint, DataCard.

Saves an immutable raw-data copy before any cleaning, sampling, or feature
engineering. Computes a SHA-256 content fingerprint and produces a schema
artifact referenced from the returned DataCard.
"""

import hashlib
import io
import json
from pathlib import Path

import pandas as pd

from athena.core.schemas import DataCard
from athena.storage.artifact_store import ArtifactStore


def _infer_schema(df: pd.DataFrame) -> dict:
    """从 DataFrame 构建轻量级列级 schema 摘要。"""
    columns = {}
    for col_name in df.columns:
        col = df[col_name]
        columns[str(col_name)] = {
            "dtype": str(col.dtype),
            "null_count": int(col.isnull().sum()),
            "unique_count": int(col.nunique()),
        }
    return {
        "columns": columns,
        "row_count": len(df),
        "column_count": len(df.columns),
    }


async def create_data_card(dataset_path: str, store: ArtifactStore) -> DataCard:
    """接入数据集：保存不可变原始副本，计算指纹，产出 schema artifact。

    在清洗、抽样或特征工程之前保存原始数据。计算文件字节的 SHA-256 指纹，
    将原始内容存入 ArtifactStore，并写入 schema 摘要 artifact。

    Args:
        dataset_path: CSV 或 Parquet 文件在磁盘上的路径。
        store: 内容寻址持久化的 ArtifactStore。

    Returns:
        包含 dataset_ref（不可变原始副本）、fingerprint 和 schema_ref 的 DataCard。
    """
    raw_path = Path(dataset_path)
    if not raw_path.exists():
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")

    raw_bytes = raw_path.read_bytes()
    fingerprint = hashlib.sha256(raw_bytes).hexdigest()

    # 保存不可变原始副本到 artifact store
    dataset_ref = await store.put_bytes(raw_bytes)

    # 从已读取的字节读取数据用于 schema 推断，避免二次磁盘 I/O
    buf = io.BytesIO(raw_bytes)
    if raw_path.suffix.lower() == ".parquet":
        df = pd.read_parquet(buf)
    else:
        df = pd.read_csv(buf)

    schema = _infer_schema(df)
    schema_json = json.dumps(schema, ensure_ascii=False)
    schema_ref = await store.put_text(schema_json)

    return DataCard(
        dataset_ref=dataset_ref,
        fingerprint=fingerprint,
        schema_ref=schema_ref,
        split_manifest_ref=None,  # 原始数据无 split manifest
    )
