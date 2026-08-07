import asyncio
from pathlib import Path

import pytest

from src.main import RunConfig, drive_runtime, parse_args, validate_config


def test_parse_args_builds_explicit_run_config(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("feature,label\n1,0\n", encoding="utf-8")

    config = parse_args(
        [
            "--data",
            str(data),
            "--target",
            "label",
            "--model",
            "openai:test-model",
            "--backend",
            "qoder",
            "--max-experiments",
            "2",
        ]
    )

    assert config.data == data
    assert config.target == "label"
    assert config.backend == "qoder"
    assert config.max_experiments == 2


def test_validate_config_rejects_missing_dataset(tmp_path: Path) -> None:
    config = RunConfig(
        data=tmp_path / "missing.csv",
        target="label",
        model="openai:test-model",
        backend="qoder",
        output_dir=tmp_path / "run",
    )

    with pytest.raises(ValueError, match="dataset does not exist"):
        validate_config(config)


class RecordingRuntime:
    def __init__(self, *, fail_search: bool = False) -> None:
        self.calls = []
        self.run_task = None
        self.phase = "IDLE"
        self.fail_search = fail_search

    async def dispatch(self, method, params):
        self.calls.append((method, params))
        if method == "TASK_CONFIGURE":
            self.phase = "CONFIGURED"
            return {"configured": True}
        if method == "SEARCH_START":

            async def finish():
                if self.fail_search:
                    self.phase = "FAILED"
                    raise RuntimeError("search failed")
                self.phase = "COMPLETED"

            self.run_task = asyncio.create_task(finish())
            return {"status": "started"}
        if method == "VALIDATE_START":
            self.phase = "VALIDATE"
            return {"sota_id": "exp-sota"}
        if method == "REPORT_GENERATE":
            self.phase = "COMPLETED"
            return {"report_ref": "artifact://reports/final.md"}
        if method == "tree_save":
            return {"saved": True, "path": params["path"]}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_drive_runtime_executes_and_persists_all_phases(tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    config = RunConfig(
        data=tmp_path / "data.csv",
        target="label",
        model="openai:test-model",
        backend="qoder",
        output_dir=tmp_path / "run",
    )

    result = await drive_runtime(runtime, config)

    methods = [method for method, _ in runtime.calls]
    assert methods == [
        "TASK_CONFIGURE",
        "SEARCH_START",
        "tree_save",
        "VALIDATE_START",
        "tree_save",
        "REPORT_GENERATE",
        "tree_save",
    ]
    assert result["report_ref"] == "artifact://reports/final.md"


@pytest.mark.asyncio
async def test_drive_runtime_saves_tree_when_search_fails(tmp_path: Path) -> None:
    runtime = RecordingRuntime(fail_search=True)
    config = RunConfig(
        data=tmp_path / "data.csv",
        target="label",
        model="openai:test-model",
        backend="qoder",
        output_dir=tmp_path / "run",
    )

    with pytest.raises(RuntimeError, match="search failed"):
        await drive_runtime(runtime, config)

    assert [method for method, _ in runtime.calls][-1] == "tree_save"
