"""Freshness guard tests for versioned experiment output archiving."""

from pathlib import Path

import pytest

from athena.core.workspace import GitWorkspaceError
from athena.research.output_freshness import (
    OutputFreshnessError,
    archive_output_roots,
    assert_output_roots,
    restore_output_roots,
)


def _write(path: Path, text: str = "data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_archive_moves_old_outputs_and_creates_empty_roots(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    _write(workdir / "predictions" / "old.csv")
    _write(workdir / "report" / "old.md")

    archive_output_roots(
        workdir,
        {"predictions": "predictions", "report": "report"},
        version="plan-1",
    )

    assert (workdir / "predictions").is_dir()
    assert not any((workdir / "predictions").iterdir())
    assert (workdir / "report").is_dir()
    assert not any((workdir / "report").iterdir())
    history = workdir.parent / "run-output-history" / "plan-1"
    assert (history / "predictions" / "old.csv").is_file()
    assert (history / "report" / "old.md").is_file()


@pytest.mark.asyncio
async def test_restore_returns_archived_outputs(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    _write(workdir / "predictions" / "old.csv")
    archive_output_roots(workdir, {"predictions": "predictions"}, version="v1")
    _write(workdir / "predictions" / "new.csv")

    restore_output_roots(workdir, {"predictions": "predictions"}, version="v1")

    assert (workdir / "predictions" / "old.csv").is_file()
    assert not (workdir / "predictions" / "new.csv").exists()


@pytest.mark.asyncio
async def test_assert_required_passes_when_new_output_exists(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    _write(workdir / "predictions" / "new.csv")

    assert_output_roots(
        workdir,
        {"predictions": "predictions"},
        required={"predictions"},
    )


@pytest.mark.asyncio
async def test_assert_required_fails_when_output_empty(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    archive_output_roots(workdir, {"predictions": "predictions"}, version="v1")

    with pytest.raises(OutputFreshnessError):
        assert_output_roots(
            workdir,
            {"predictions": "predictions"},
            required={"predictions"},
        )


@pytest.mark.asyncio
async def test_optional_report_may_be_absent(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    _write(workdir / "predictions" / "new.csv")

    assert_output_roots(
        workdir,
        {"predictions": "predictions", "report": "report"},
        required={"predictions"},
    )


@pytest.mark.asyncio
async def test_invalid_version_rejected(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    with pytest.raises(ValueError):
        archive_output_roots(
            workdir,
            {"predictions": "predictions"},
            version="../escape",
        )


@pytest.mark.asyncio
async def test_path_escape_rejected(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    with pytest.raises(GitWorkspaceError):
        archive_output_roots(
            workdir,
            {"predictions": "../../outside"},
            version="v1",
        )


@pytest.mark.asyncio
async def test_archive_only_touches_declared_outputs(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    _write(workdir / "source" / "model.py")
    _write(workdir / "predictions" / "old.csv")

    archive_output_roots(workdir, {"predictions": "predictions"}, version="v1")

    assert (workdir / "source" / "model.py").is_file()
    assert (workdir / "predictions").is_dir()
    assert not any((workdir / "predictions").iterdir())
