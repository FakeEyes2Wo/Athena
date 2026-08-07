import asyncio
import json
from pathlib import Path

import pytest

from src.main import (
    Application,
    RunConfig,
    drive_runtime,
    main,
    parse_args,
    run,
    validate_config,
)


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


def test_build_application_injects_trusted_runtime_and_phase_inputs(
    tmp_path: Path,
) -> None:
    from athena.code.execution import LocalExperimentRuntime
    from athena.evaluation.trusted import TrustedEvaluator
    from athena.evaluation.types import EvaluationInputs
    from src.main import build_application

    config = RunConfig(
        data=tmp_path / "data.csv",
        target="label",
        model="test",
        backend="qoder",
        output_dir=tmp_path / "run",
    )
    application = build_application(config, backends={})

    validator = application.runtime._dependencies.validator
    code_agent = validator._code_agent
    assert isinstance(code_agent._runtime, LocalExperimentRuntime)
    assert isinstance(code_agent._evaluator, TrustedEvaluator)

    validation_inputs = EvaluationInputs(
        phase="validation",
        train_path=tmp_path / "train.csv",
        features_path=tmp_path / "validation.csv",
        labels_path=tmp_path / "validation_labels.csv",
        target="label",
    )
    test_inputs = EvaluationInputs(
        phase="test",
        train_path=tmp_path / "train.csv",
        features_path=tmp_path / "test.csv",
        labels_path=tmp_path / "test_labels.csv",
        target="label",
    )
    validator.set_inputs(validation_inputs, test_inputs)
    assert validator._validation_inputs is validation_inputs
    assert validator._test_inputs is test_inputs


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


def test_parse_args_defaults_local_execution(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("feature,label\n0,0\n1,1\n", encoding="utf-8")
    config = parse_args(["--data", str(data), "--target", "label", "--model", "m"])
    assert config.execution == "local"


def test_validate_config_rejects_unsupported_metric(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("feature,label\n0,0\n1,1\n", encoding="utf-8")
    config = RunConfig(
        data=data,
        target="label",
        model="m",
        backend="qoder",
        output_dir=tmp_path / "run",
        metric="not_a_metric",
    )
    with pytest.raises(ValueError, match="unsupported"):
        validate_config(config)


def test_validate_config_rejects_target_missing_from_header(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("feature,other\n0,0\n1,1\n", encoding="utf-8")
    config = RunConfig(
        data=data,
        target="label",
        model="m",
        backend="qoder",
        output_dir=tmp_path / "run",
    )
    with pytest.raises(ValueError, match="target column not found"):
        validate_config(config)


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


def test_main_invalid_config_returns_two_without_traceback(
    tmp_path: Path, capsys
) -> None:
    code = main(
        [
            "--data",
            str(tmp_path / "missing.csv"),
            "--target",
            "label",
            "--model",
            "openai:test-model",
        ]
    )

    assert code == 2
    captured = capsys.readouterr()
    assert "invalid configuration" in captured.err
    assert "Traceback" not in captured.err


def test_main_success_returns_zero_and_warns_once(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    data = tmp_path / "data.csv"
    data.write_text(
        "feature,label\n" + "\n".join(f"r{i},{i % 2}" for i in range(10)),
        encoding="utf-8",
    )
    summary = {
        "phase": "COMPLETED",
        "sota_id": "exp-1",
        "tree_path": str(tmp_path / "run" / "research_tree.json"),
        "report_ref": "artifact://reports/final.md",
        "backend": "qoder",
        "execution": "local",
        "strong_isolation": False,
        "budget": {},
    }

    async def fake_run(config):
        return summary

    monkeypatch.setattr("src.main.run", fake_run)

    code = main(
        [
            "--data",
            str(data),
            "--target",
            "label",
            "--model",
            "openai:test-model",
            "--backend",
            "qoder",
            "--output-dir",
            str(tmp_path / "run"),
        ]
    )

    assert code == 0
    captured = capsys.readouterr()
    assert captured.err.count("local mode") == 1
    assert "running in local mode" in captured.err
    assert '"report_ref": "artifact://reports/final.md"' in captured.out


class _StubTree:
    def best_experiment_id(self) -> str:
        return "exp-1"


class _StubBudget:
    def model_dump(self, mode: str = "json") -> dict:
        return {"max_experiments": 1}


class _StubRuntime:
    """Minimal runtime surface for run()'s summary assembly."""

    def __init__(self) -> None:
        self.phase = "COMPLETED"
        self.tree = _StubTree()
        self.budget = _StubBudget()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_run_writes_local_summary_file(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data.csv"
    data.write_text(
        "feature,label\n" + "\n".join(f"r{i},{i % 2}" for i in range(10)),
        encoding="utf-8",
    )
    config = RunConfig(
        data=data,
        target="label",
        model="openai:test-model",
        backend="qoder",
        output_dir=tmp_path / "run",
    )
    application = Application(runtime=_StubRuntime(), context=object())
    monkeypatch.setattr("src.main.build_application", lambda _config: application)

    async def fake_drive(runtime, config):
        config.output_dir.resolve().mkdir(parents=True, exist_ok=True)
        return {
            "report_ref": "artifact://reports/final.md",
            "tree_path": str(config.output_dir / "research_tree.json"),
        }

    monkeypatch.setattr("src.main.drive_runtime", fake_drive)

    summary = await run(config)

    assert summary["execution"] == "local"
    assert summary["strong_isolation"] is False
    saved = json.loads(
        (config.output_dir / "run_summary.json").read_text(encoding="utf-8")
    )
    assert saved["execution"] == "local"
    assert saved["strong_isolation"] is False


def test_validate_config_rejects_too_few_target_rows(tmp_path: Path) -> None:
    data = tmp_path / "data.csv"
    data.write_text("feature,label\n0,1\n1,0\n2,1\n3,0\n", encoding="utf-8")
    config = RunConfig(
        data=data,
        target="label",
        model="m",
        backend="qoder",
        output_dir=tmp_path / "run",
    )
    with pytest.raises(ValueError, match="non-empty train, validation, and test"):
        validate_config(config)
