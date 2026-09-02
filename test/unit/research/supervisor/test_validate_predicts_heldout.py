"""VALIDATE must score the held-out split, not the one SEARCH already used."""

import json
from pathlib import Path

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.workspace import GitWorkBranch
from athena.research.supervisor.validation import _execute_predictions

ROW_ID = "__athena_row_id"


def _features(path: Path, ids: range) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(f"{i},{i * 0.5}" for i in ids)
    path.write_text(f"{ROW_ID},feature\n{rows}\n", encoding="utf-8")
    return path


def _predictions(workdir: Path, ids: range) -> Path:
    out = workdir / "predictions"
    out.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(f"{i},0.4" for i in ids)
    (out / "pred.csv").write_text(f"{ROW_ID},score\n{rows}\n", encoding="utf-8")
    return out


class _Git:
    def __init__(self) -> None:
        self.restored: list[tuple[str, ...]] = []

    async def restore_paths(self, workspace, paths) -> None:
        del workspace
        self.restored.append(tuple(paths))


class _Execution:
    """Records ATHENA_PREDICT_FEATURES seen by each command and emits predictions."""

    def __init__(self, produce_ids: range | None = None) -> None:
        self.produce_ids = produce_ids
        self.predict_features_seen: list[str | None] = []
        self.data_csv_seen: list[str | None] = []

    async def run(self, context, command=None, *, argv=None, **kwargs):
        del context, argv, kwargs
        request = command
        self.predict_features_seen.append(str(request.predict_features) if request.predict_features else None)
        self.data_csv_seen.append(str(request.data_csv) if request.data_csv else None)
        if self.produce_ids is not None:
            workdir = Path(request.workdir)
            _predictions(workdir, self.produce_ids)

        class _Ok:
            ok = True
            stdout = ""
            stderr = ""
            exit_code = 0

        return _Ok()


def _workspace(tmp_path: Path) -> tuple[Path, GitWorkBranch]:
    workdir = tmp_path / "validate"
    workdir.mkdir(parents=True)
    (workdir / "experiment.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [["python", "solution/train_model.py"]],
                "outputs": {"predictions": "predictions", "report": "REPORT.md"},
            }
        ),
        encoding="utf-8",
    )
    return workdir, GitWorkBranch(
        path=str(workdir), branch="validate", base_commit="sota-a"
    )


@pytest.mark.asyncio
async def test_the_rerun_is_pointed_at_the_held_out_split(tmp_path: Path) -> None:
    final = _features(tmp_path / "data_split" / "final_features.csv", range(100, 110))
    workdir, workspace = _workspace(tmp_path)
    execution = _Execution(produce_ids=range(100, 110))

    await _execute_predictions(
        execution=execution,
        git=_Git(),
        workspace=workspace,
        store=LocalArtifactStore(tmp_path / "artifacts"),
        publish=None,
        predict_features=final,
    )

    assert execution.predict_features_seen == [str(final)]


@pytest.mark.asyncio
async def test_the_rerun_receives_the_configured_dataset_path(tmp_path: Path) -> None:
    dataset = tmp_path / "model_input.csv"
    dataset.write_text("feature,label\n1,0\n", encoding="utf-8")
    workdir, workspace = _workspace(tmp_path)
    execution = _Execution(produce_ids=range(0, 1))

    await _execute_predictions(
        execution=execution,
        git=_Git(),
        workspace=workspace,
        store=LocalArtifactStore(tmp_path / "artifacts"),
        publish=None,
        data_csv=dataset,
    )

    assert execution.data_csv_seen == [str(dataset)]


@pytest.mark.asyncio
async def test_a_hardcoded_path_fails_with_the_real_reason(tmp_path: Path) -> None:
    """Zero overlap must say so, instead of pretending the evaluator is broken."""
    final = _features(tmp_path / "data_split" / "final_features.csv", range(100, 110))
    _, workspace = _workspace(tmp_path)

    with pytest.raises(ValueError) as caught:
        await _execute_predictions(
            execution=_Execution(produce_ids=range(0, 10)),
            git=_Git(),
            workspace=workspace,
            store=LocalArtifactStore(tmp_path / "artifacts"),
            publish=None,
            predict_features=final,
        )

    message = str(caught.value)
    assert "10 of 10" in message
    assert "overlap 0" in message
    assert "ATHENA_PREDICT_FEATURES" in message


@pytest.mark.asyncio
async def test_full_coverage_passes(tmp_path: Path) -> None:
    final = _features(tmp_path / "data_split" / "final_features.csv", range(100, 110))
    _, workspace = _workspace(tmp_path)

    run = await _execute_predictions(
        execution=_Execution(produce_ids=range(100, 110)),
        git=_Git(),
        workspace=workspace,
        store=LocalArtifactStore(tmp_path / "artifacts"),
        publish=None,
        predict_features=final,
    )

    assert run.predictions_path == "predictions"
    assert run.predictions_ref
