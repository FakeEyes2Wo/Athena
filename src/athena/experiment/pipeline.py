"""PREPARE phase state machine: SAMPLING→EDA→CLEANING→SPLITTING→DONE."""

from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path
from typing import Any

import pandas as pd

from athena.core.contracts import ArtifactRef
from athena.data.operations import split_dataset
from athena.data.types import ColumnSummary, DataProfile, ProcessingLog


def prepare_dataset(
    raw_frame: pd.DataFrame,
    cleaned_frame: pd.DataFrame,
    *,
    seed: int,
    validation_ratio: float,
    test_ratio: float,
    put_frame: Callable[[pd.DataFrame], ArtifactRef],
) -> ProcessingLog:
    raw_copy = put_frame(raw_frame.copy())
    cleaned_data = put_frame(cleaned_frame.copy())
    manifest = split_dataset(
        cleaned_frame,
        seed=seed,
        validation_ratio=validation_ratio,
        test_ratio=test_ratio,
        put_frame=put_frame,
    )
    return ProcessingLog(
        raw_copy=raw_copy,
        cleaned_data=cleaned_data,
        splits={
            "train": manifest.train,
            "validation": manifest.validation,
            "test": manifest.test,
        },
    )


def search_input_refs(
    log: ProcessingLog,
) -> tuple[ArtifactRef, ArtifactRef, ArtifactRef]:
    return log.cleaned_data, log.splits["train"], log.splits["validation"]


class Phase(Enum):
    SAMPLING = auto()
    EDA = auto()
    CLEANING = auto()
    SPLITTING = auto()
    DONE = auto()


class PipelineError(RuntimeError):
    """Pipeline phase failure after exhausting retries."""


_PHASE_ORDER = [
    Phase.SAMPLING,
    Phase.EDA,
    Phase.CLEANING,
    Phase.SPLITTING,
    Phase.DONE,
]


class DataPipeline:
    """PREPARE state machine. Orchestrates DataAgent through phases."""

    def __init__(self, data_agent, plot_agent=None):
        self._data_agent = data_agent
        self._plot_agent = plot_agent
        self._phase = Phase.SAMPLING
        self._retries: dict[Phase, int] = {}
        self._profile: DataProfile | None = None

    async def run(self, data_path: str) -> DataProfile:
        """Run PREPARE and return an observed profile, never a placeholder."""
        path = Path(data_path)
        workspace = path.parent if path.is_file() else path
        workspace.mkdir(parents=True, exist_ok=True)
        self._phase = Phase.SAMPLING
        self._retries.clear()
        self._profile = None
        while self._phase != Phase.DONE:
            try:
                result = await self._execute_phase(str(workspace))
                profile = self._coerce_profile(result)
                if profile is not None:
                    self._profile = profile
                self._advance()
            except Exception as exc:
                phase = self._phase
                self._retries[phase] = self._retries.get(phase, 0) + 1
                if self._retries[phase] >= 3:
                    raise PipelineError(
                        f"Phase {phase} failed after 3 retries"
                    ) from exc
                self._rollback()
        if self._profile is not None:
            return self._profile
        source = self._find_profile_source(path, workspace)
        if source is None:
            raise PipelineError(
                "PREPARE completed without a DataProfile or a readable CSV artifact"
            )
        return self._profile_csv(source)

    async def _execute_phase(self, data_path: str) -> Any:
        phase = self._phase
        if phase == Phase.SAMPLING:
            task = (
                "Inspect dataset. If >100K rows, create 3 samples "
                "with seeds 17/42/97. Otherwise begin EDA."
            )
        elif phase == Phase.EDA:
            task = (
                "Write analysis scripts. Investigate distributions, "
                "correlations, missing patterns, outliers. Produce "
                "EDA.md draft with [PLOT: ...] directives."
            )
        elif phase == Phase.CLEANING:
            task = (
                "Clean data: fill missing values, handle outliers, "
                "apply encodings. Record every operation in "
                "feature_process.csv."
            )
        elif phase == Phase.SPLITTING:
            task = (
                "Split data into train/val/test sets. "
                "Ensure no overlap. Record split manifest."
            )
        else:
            return
        task = f"{task}\nWorkspace: {data_path}"
        result = await self._data_agent.run(task)
        if phase == Phase.EDA and self._plot_agent:
            await self._plot_agent.run(
                "Read EDA.md, generate all [PLOT: ...] charts, "
                f"backfill references.\nWorkspace: {data_path}"
            )
        return result

    @staticmethod
    def _coerce_profile(result: Any) -> DataProfile | None:
        """Accept the canonical profile model or its serialized representation."""
        if isinstance(result, DataProfile):
            return result
        if isinstance(result, dict):
            return DataProfile.model_validate(result)
        return None

    @staticmethod
    def _find_profile_source(path: Path, workspace: Path) -> Path | None:
        """Choose a deterministic CSV artifact for final profiling."""
        if path.is_file() and path.suffix.lower() == ".csv":
            return path
        preferred = ("cleaned.csv", "train.csv", "dataset.csv", "data.csv")
        for name in preferred:
            candidate = workspace / name
            if candidate.is_file():
                return candidate
        return next(iter(sorted(workspace.glob("*.csv"))), None)

    @staticmethod
    def _profile_csv(path: Path) -> DataProfile:
        """Build a compact profile from a CSV artifact produced by PREPARE."""
        frame = pd.read_csv(path)
        columns = [
            ColumnSummary(
                name=str(name),
                dtype=str(series.dtype),
                missing_rate=float(series.isna().mean()),
                n_unique=int(series.nunique(dropna=True)),
                sample_values=[
                    str(value) for value in series.dropna().head(5).tolist()
                ],
            )
            for name, series in frame.items()
        ]
        issues = [
            f"{column.name}: {column.missing_rate:.1%} missing"
            for column in columns
            if column.missing_rate > 0
        ]
        return DataProfile(
            row_count=len(frame),
            col_count=len(frame.columns),
            columns=columns,
            missing_rate=float(frame.isna().mean().mean()) if columns else 0.0,
            issue_summary="; ".join(issues),
        )

    def _advance(self) -> None:
        idx = _PHASE_ORDER.index(self._phase)
        self._phase = _PHASE_ORDER[idx + 1]

    def _rollback(self) -> None:
        idx = _PHASE_ORDER.index(self._phase)
        if idx > 0:
            self._phase = _PHASE_ORDER[idx - 1]
