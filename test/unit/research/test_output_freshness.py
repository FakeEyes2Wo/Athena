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


# --------------------------------------------------------------------------- #
# 文件型声明产物。真实 manifest 用 {"predictions": "predictions", "report":
# "REPORT.md"}——一个目录一个文件。原实现无条件 mkdir，把 REPORT.md 变成目录，
# 命令再写它就 PermissionError，PREPARE 永远交不出 report。上面那批测试全用
# 目录，所以谁都没发现。
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_file_output_stays_a_file(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    _write(workdir / "REPORT.md", "# old\n")

    archive_output_roots(workdir, {"report": "REPORT.md"}, version="v1")

    assert not (workdir / "REPORT.md").is_dir(), "文件型产物被换成了目录"
    # 归档后命令必须还能写它。
    (workdir / "REPORT.md").write_text("# new\n", encoding="utf-8")
    assert (workdir / "REPORT.md").read_text(encoding="utf-8") == "# new\n"
    history = workdir.parent / "run-output-history" / "v1"
    assert (history / "REPORT.md").read_text(encoding="utf-8") == "# old\n"


@pytest.mark.asyncio
async def test_assert_accepts_a_non_empty_file_output(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    _write(workdir / "predictions" / "new.csv")
    _write(workdir / "REPORT.md", "# report\n")

    assert_output_roots(
        workdir,
        {"predictions": "predictions", "report": "REPORT.md"},
        required={"predictions", "report"},
    )


@pytest.mark.asyncio
async def test_assert_rejects_an_empty_file_output(tmp_path: Path) -> None:
    """空文件和空目录一样，都是"命令什么也没产出"。"""
    workdir = tmp_path / "run"
    _write(workdir / "REPORT.md", "")

    with pytest.raises(OutputFreshnessError):
        assert_output_roots(workdir, {"report": "REPORT.md"}, required={"report"})


@pytest.mark.asyncio
async def test_missing_file_output_after_archive_is_reported(tmp_path: Path) -> None:
    """归档之后没人写回来，必须报出来，而不是当成通过。"""
    workdir = tmp_path / "run"
    _write(workdir / "REPORT.md", "# old\n")
    archive_output_roots(workdir, {"report": "REPORT.md"}, version="v1")

    with pytest.raises(OutputFreshnessError):
        assert_output_roots(workdir, {"report": "REPORT.md"}, required={"report"})


@pytest.mark.asyncio
async def test_restore_brings_back_a_file_output(tmp_path: Path) -> None:
    workdir = tmp_path / "run"
    _write(workdir / "REPORT.md", "# old\n")
    archive_output_roots(workdir, {"report": "REPORT.md"}, version="v1")
    _write(workdir / "REPORT.md", "# new\n")

    restore_output_roots(workdir, {"report": "REPORT.md"}, version="v1")

    assert (workdir / "REPORT.md").read_text(encoding="utf-8") == "# old\n"
