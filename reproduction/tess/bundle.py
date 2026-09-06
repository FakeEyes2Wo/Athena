"""Portable JSON model-bundle metadata and checksum handling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .features import CAND_FEATURES

BUNDLE_VERSION = 1
FROZEN_THRESHOLD = 0.41


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bundle_metadata(
    model_dir: Path,
    *,
    model_file: str,
    imputer_statistics: list[float],
    model_params: dict[str, Any],
    device: str,
    provenance: Any,
) -> None:
    model_path = model_dir / model_file
    metadata_path = model_dir / "metadata.json"
    model_sha = sha256_file(model_path)
    payload = {
        "bundle_version": BUNDLE_VERSION,
        "task_id": "TESS_flare_win120min",
        "model_file": model_file,
        "feature_order": list(CAND_FEATURES),
        "imputer_strategy": "median",
        "imputer_statistics": [float(value) for value in imputer_statistics],
        "threshold": FROZEN_THRESHOLD,
        "model_sha256": model_sha,
        "model_params": model_params,
        "device": device,
        "provenance": provenance,
    }
    metadata_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    metadata_sha = sha256_file(metadata_path)
    (model_dir / "checksums.txt").write_text(
        f"{model_sha}  {model_file}\n{metadata_sha}  metadata.json\n",
        encoding="utf-8",
    )


def load_verified_metadata(model_dir: Path) -> dict[str, Any]:
    """Load metadata only after validating both bundle checksums."""
    metadata_path = model_dir / "metadata.json"
    checksums_path = model_dir / "checksums.txt"
    if not metadata_path.is_file() or not checksums_path.is_file():
        raise ValueError("model bundle is missing metadata.json or checksums.txt")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("model bundle metadata is invalid") from exc
    if not isinstance(metadata, dict):
        raise ValueError("model bundle metadata must be an object")
    expected: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            expected[parts[1]] = parts[0].lower()
    model_file = metadata.get("model_file", "model.json")
    if not isinstance(model_file, str) or Path(model_file).name != model_file:
        raise ValueError("model bundle model_file must be a simple filename")
    for filename in (model_file, "metadata.json"):
        path = model_dir / filename
        if filename not in expected or not path.is_file():
            raise ValueError(f"model bundle checksum is missing for {filename}")
        if sha256_file(path) != expected[filename]:
            raise ValueError(f"model bundle checksum mismatch for {filename}")
    if metadata.get("model_sha256") != expected[model_file]:
        raise ValueError("model bundle metadata model_sha256 does not match checksum")
    if metadata.get("bundle_version") != BUNDLE_VERSION:
        raise ValueError("unsupported model bundle version")
    if metadata.get("task_id") != "TESS_flare_win120min":
        raise ValueError("model bundle task_id is not TESS_flare_win120min")
    if metadata.get("feature_order") != list(CAND_FEATURES):
        raise ValueError(
            "model bundle feature order does not match the frozen contract"
        )
    if metadata.get("threshold") != FROZEN_THRESHOLD:
        raise ValueError("model bundle threshold is not the frozen 0.41")
    statistics = metadata.get("imputer_statistics")
    if not isinstance(statistics, list) or len(statistics) != len(CAND_FEATURES):
        raise ValueError(
            "model bundle imputer statistics do not match the feature order"
        )
    return metadata
