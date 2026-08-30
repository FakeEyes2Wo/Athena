"""Deterministic platform-owned data splitting for scientific isolation.

This is the foundation for train/search/final isolation: the platform, not the
evaluator agent, should own the split so candidates never see held-out labels.
The current research runtime still relies on LLM-created evaluator splits; this
module provides the deterministic primitive to replace that step.
"""

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Sequence

#: Written beside the split files so a finished project can say how it split.
SPLIT_MANIFEST_NAME = "split_manifest.json"


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
    groups: Sequence[str] | None = None,
) -> SplitManifest:
    """Split row ids deterministically into train/search/final sets.

    Fractions are applied to the shuffled order. The remaining fraction is the
    train set. Raises when fractions are outside [0, 1) or too large together.

    ``groups`` gives each row a grouping key (one entry per row id, same order).
    When present, rows sharing a key always land in the same split. Without it
    a plain row-level shuffle silently leaks between splits for any dataset
    whose rows are not independent -- consecutive frames of one active region,
    windows cut from one star's light curve, repeated measurements of one
    patient. The metric still goes up in that case; it just stops meaning
    anything, and nothing downstream can detect it.
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
    if groups is None:
        units: list[tuple[str, ...]] = [(row_id,) for row_id in ids]
    else:
        keys = list(groups)
        if len(keys) != len(ids):
            raise ValueError("groups must have one entry per row id")
        members: dict[str, list[str]] = {}
        for row_id, key in zip(ids, keys):
            members.setdefault(str(key), []).append(row_id)
        # Sort before shuffling so the split depends on the seed alone, not on
        # whatever order dict insertion happened to produce.
        units = [tuple(members[key]) for key in sorted(members)]

    shuffled = units[:]
    rng.shuffle(shuffled)

    # Fractions are over rows, not units: with uneven group sizes, cutting on
    # unit counts would hand a wildly wrong share of the data to each split.
    # Targets are floored exactly like the ungrouped path used to slice, so
    # single-row groups reproduce the previous split byte for byte; a group
    # larger than the remaining budget overshoots, which is unavoidable.
    total = len(ids)
    want_search = int(total * search_frac)
    want_final = int(total * final_frac)
    search: list[str] = []
    final: list[str] = []
    train: list[str] = []
    for unit in shuffled:
        if len(search) < want_search:
            search.extend(unit)
        elif len(final) < want_final:
            final.extend(unit)
        else:
            train.extend(unit)
    manifest = SplitManifest(
        train_ids=tuple(train), search_ids=tuple(search), final_ids=tuple(final)
    )
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
    group_column: str | None = None,
) -> SplitManifest:
    """Read a local CSV and write platform-owned train/search/final files.

    Row identity is the row index (0-based), matching the existing
    ``__athena_row_id`` convention. The target column is removed from the
    feature files, so search/final labels are never visible to candidates.

    ``group_column`` names a column whose value keeps related rows together
    (active region id, star id, subject id). Rows sharing a value never span
    two splits.
    """
    with source_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [
            name.strip() for name in (reader.fieldnames or []) if name.strip()
        ]
        if target_column not in fieldnames:
            raise ValueError(
                f"target column {target_column!r} not found in {source_csv}"
            )
        if group_column is not None and group_column not in fieldnames:
            raise ValueError(f"group column {group_column!r} not found in {source_csv}")
        rows = [dict(row) for row in reader]

    ids = [str(index) for index in range(len(rows))]
    groups = (
        [str(row.get(group_column, "")) for row in rows]
        if group_column is not None
        else None
    )
    manifest = split_ids(
        ids,
        search_frac=search_frac,
        final_frac=final_frac,
        seed=seed,
        groups=groups,
    )
    by_id = dict(zip(ids, rows))
    feature_fields = [
        "__athena_row_id",
        *(field for field in fieldnames if field != target_column),
    ]
    label_fields = ["__athena_row_id", target_column]

    def feature_rows(id_set: Sequence[str]) -> list[dict[str, str]]:
        return [{"__athena_row_id": row_id, **by_id[row_id]} for row_id in id_set]

    def label_rows(id_set: Sequence[str]) -> list[dict[str, str]]:
        return [
            {
                "__athena_row_id": row_id,
                target_column: by_id[row_id][target_column],
            }
            for row_id in id_set
        ]

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        output_dir / "train.csv", fieldnames, [by_id[i] for i in manifest.train_ids]
    )
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
    _write_split_manifest(
        output_dir,
        source_csv=source_csv,
        target_column=target_column,
        search_frac=search_frac,
        final_frac=final_frac,
        seed=seed,
        group_column=group_column,
    )
    return manifest


def _write_split_manifest(
    output_dir: Path,
    *,
    source_csv: Path,
    target_column: str,
    search_frac: float,
    final_frac: float,
    seed: int,
    group_column: str | None,
) -> Path:
    """Record how this split was made, next to the files it made.

    The split is fully determined by (source bytes, target, group column,
    fractions, seed) -- but none of those were written down anywhere, and
    ``state.json`` does not carry them either. So a finished project could not
    say which split its frozen evaluator was scoring against.

    Real cost (2026-08-30): reconstructing a run's split needed a brute-force
    sweep over candidate seeds, comparing row-id sets until one matched at 62.
    That only worked because the source CSV was still byte-identical; had it
    moved or been regenerated, the run's numbers would have been unfalsifiable.
    """
    payload = {
        "source_csv": {
            "path": str(source_csv.resolve()),
            "sha256": _sha256(source_csv),
        },
        "params": {
            "target_column": target_column,
            "group_column": group_column,
            "search_frac": search_frac,
            "final_frac": final_frac,
            "seed": seed,
        },
        "files": {
            path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in sorted(output_dir.glob("*.csv"))
        },
    }
    manifest_path = output_dir / SPLIT_MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
