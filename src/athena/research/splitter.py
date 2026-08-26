"""Deterministic platform-owned data splitting for scientific isolation.

This is the foundation for train/search/final isolation: the platform, not the
evaluator agent, should own the split so candidates never see held-out labels.
The current research runtime still relies on LLM-created evaluator splits; this
module provides the deterministic primitive to replace that step.
"""

import csv
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Sequence


@dataclass(frozen=True)
class SplitManifest:
    """Disjoint train/search/final row-id sets produced by the platform."""

    train_ids: tuple[str, ...]
    search_ids: tuple[str, ...]
    final_ids: tuple[str, ...]

    def validate(self) -> None:
        train = set(self.train_ids)
        search = set(self.search_ids)
        final = set(self.final_ids)
        if len(train) != len(self.train_ids):
            raise ValueError("train_ids contains duplicates")
        if len(search) != len(self.search_ids):
            raise ValueError("search_ids contains duplicates")
        if len(final) != len(self.final_ids):
            raise ValueError("final_ids contains duplicates")
        if train & search or train & final or search & final:
            raise ValueError("train/search/final splits must be disjoint")


def split_ids(
    row_ids: Sequence[str],
    *,
    search_frac: float = 0.2,
    final_frac: float = 0.2,
    seed: int = 0,
) -> SplitManifest:
    """Split row ids deterministically into train/search/final sets.

    Fractions are applied to the shuffled order. The remaining fraction is the
    train set. Raises when fractions are outside [0, 1) or too large together.
    """
    if not 0 <= search_frac < 1:
        raise ValueError("search_frac must be in [0, 1)")
    if not 0 <= final_frac < 1:
        raise ValueError("final_frac must be in [0, 1)")
    if search_frac + final_frac >= 1:
        raise ValueError("search_frac + final_frac must be less than 1")

    ids = list(row_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("row_ids must be unique")

    rng = Random(seed)
    shuffled = ids[:]
    rng.shuffle(shuffled)

    n = len(shuffled)
    n_search = int(n * search_frac)
    n_final = int(n * final_frac)
    search = tuple(shuffled[:n_search])
    final = tuple(shuffled[n_search : n_search + n_final])
    train = tuple(shuffled[n_search + n_final :])
    manifest = SplitManifest(train_ids=train, search_ids=search, final_ids=final)
    manifest.validate()
    return manifest


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def materialize_csv_split(
    source_csv: Path,
    output_dir: Path,
    target_column: str,
    *,
    search_frac: float = 0.2,
    final_frac: float = 0.2,
    seed: int = 0,
) -> SplitManifest:
    """Read a local CSV and write platform-owned train/search/final files.

    Row identity is the row index (0-based), matching the existing
    ``__athena_row_id`` convention. The target column is removed from the
    feature files, so search/final labels are never visible to candidates.
    """
    with source_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [name.strip() for name in (reader.fieldnames or []) if name.strip()]
        if target_column not in fieldnames:
            raise ValueError(f"target column {target_column!r} not found in {source_csv}")
        rows = [dict(row) for row in reader]

    ids = [str(index) for index in range(len(rows))]
    manifest = split_ids(
        ids,
        search_frac=search_frac,
        final_frac=final_frac,
        seed=seed,
    )
    by_id = dict(zip(ids, rows))
    feature_fields = [
        "__athena_row_id",
        *(field for field in fieldnames if field != target_column),
    ]
    label_fields = ["__athena_row_id", target_column]

    def feature_rows(id_set: Sequence[str]) -> list[dict[str, str]]:
        return [
            {"__athena_row_id": row_id, **by_id[row_id]}
            for row_id in id_set
        ]

    def label_rows(id_set: Sequence[str]) -> list[dict[str, str]]:
        return [
            {
                "__athena_row_id": row_id,
                target_column: by_id[row_id][target_column],
            }
            for row_id in id_set
        ]

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "train.csv", fieldnames, [by_id[i] for i in manifest.train_ids])
    _write_csv(
        output_dir / "search_features.csv",
        feature_fields,
        feature_rows(manifest.search_ids),
    )
    _write_csv(
        output_dir / "search_labels.csv",
        label_fields,
        label_rows(manifest.search_ids),
    )
    _write_csv(
        output_dir / "final_features.csv",
        feature_fields,
        feature_rows(manifest.final_ids),
    )
    _write_csv(
        output_dir / "final_labels.csv",
        label_fields,
        label_rows(manifest.final_ids),
    )
    return manifest
