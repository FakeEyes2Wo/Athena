"""Experiment runtime contract tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from athena.code.execution import (
    DockerExperimentRuntime,
    ExecutionRequest,
    LocalExperimentRuntime,
)
from athena.code.types import ExecutionOutput


class RecordingRunner:
    def __init__(self, outputs: list[ExecutionOutput]) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[tuple[str, ...], Path | None, dict[str, str], int]] = []

    async def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None,
        environment: dict[str, str],
        timeout_s: int,
    ) -> ExecutionOutput:
        self.calls.append((command, cwd, environment, timeout_s))
        return self.outputs.pop(0)


def _output(returncode: int = 0, stderr: str = "") -> ExecutionOutput:
    return ExecutionOutput(stdout="", stderr=stderr, returncode=returncode)


@pytest.mark.asyncio
async def test_local_runtime_uses_allowlisted_environment(tmp_path: Path) -> None:
    script = tmp_path / "run_experiment.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "print(json.dumps({'env': dict(os.environ), "
        "'cwd': str(pathlib.Path.cwd()), 'python': sys.executable}))\n",
        encoding="utf-8",
    )
    runtime = LocalExperimentRuntime(
        base_environment={
            "Path": "safe-path",
            "SYSTEMROOT": "safe-root",
            "OPENAI_API_KEY": "must-not-leak",
            "ATHENA_FROM_PARENT": "must-not-leak",
        }
    )

    result = await runtime.run(
        ExecutionRequest(
            entrypoint="run_experiment.py",
            cwd=tmp_path,
            timeout_s=10,
            environment={
                "ATHENA_SPLIT_MANIFEST": ".athena/phase_manifest.json",
                "QODER_API_KEY": "must-not-leak",
            },
            readonly_inputs=(),
        )
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["cwd"] == str(tmp_path.resolve())
    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert payload["env"] == {
        "ATHENA_SPLIT_MANIFEST": ".athena/phase_manifest.json",
        "PATH": "safe-path",
        "SYSTEMROOT": "safe-root",
    }


@pytest.mark.asyncio
async def test_local_runtime_rejects_entrypoint_outside_cwd(tmp_path: Path) -> None:
    runtime = LocalExperimentRuntime(base_environment={})

    with pytest.raises(ValueError, match="entrypoint must stay within cwd"):
        await runtime.run(
            ExecutionRequest(
                entrypoint="../outside.py",
                cwd=tmp_path,
                timeout_s=10,
                environment={},
                readonly_inputs=(),
            )
        )


@pytest.mark.asyncio
async def test_local_runtime_timeout_returns_failed_output(tmp_path: Path) -> None:
    (tmp_path / "run_experiment.py").write_text(
        "import time\ntime.sleep(10)\n", encoding="utf-8"
    )

    result = await LocalExperimentRuntime(base_environment={}).run(
        ExecutionRequest(
            entrypoint="run_experiment.py",
            cwd=tmp_path,
            timeout_s=1,
            environment={},
            readonly_inputs=(),
        )
    )

    assert result.returncode != 0
    assert "timeout" in result.stderr.lower()


@pytest.mark.asyncio
async def test_docker_runtime_builds_isolated_command(tmp_path: Path) -> None:
    input_path = tmp_path / "predict.csv"
    input_path.write_text("id,value\n1,2\n", encoding="utf-8")
    runner = RecordingRunner([_output()])
    runtime = DockerExperimentRuntime(
        "athena-experiment:test",
        command_runner=runner,
        executable_locator=lambda _: "C:/Program Files/Docker/docker.exe",
        base_environment={"Path": "safe-path", "OPENAI_API_KEY": "must-not-leak"},
    )

    result = await runtime.run(
        ExecutionRequest(
            entrypoint="run_experiment.py",
            cwd=tmp_path,
            timeout_s=25,
            environment={
                "ATHENA_SPLIT_MANIFEST": ".athena/phase_manifest.json",
                "OPENAI_API_KEY": "must-not-leak",
            },
            readonly_inputs=(input_path,),
        )
    )

    assert result.returncode == 0
    command, cwd, environment, timeout_s = runner.calls[0]
    assert command[:12] == (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "65532:65532",
        "--memory",
        "2g",
        "--cpus",
        "2",
    )
    assert command[12:14] == ("--pids-limit", "256")
    assert f"type=bind,src={tmp_path.resolve()},dst=/workspace" in command
    assert (
        f"type=bind,src={input_path.resolve()},"
        "dst=/workspace/.athena/inputs/predict.csv,readonly"
    ) in command
    assert "ATHENA_SPLIT_MANIFEST=.athena/phase_manifest.json" in command
    assert all("OPENAI_API_KEY" not in argument for argument in command)
    assert command[-2:] == (
        "athena-experiment:test",
        "run_experiment.py",
    )
    assert cwd == tmp_path.resolve()
    assert environment == {"PATH": "safe-path"}
    assert timeout_s == 25


@pytest.mark.asyncio
async def test_docker_preflight_reports_missing_executable() -> None:
    runtime = DockerExperimentRuntime(
        "athena-experiment:test", executable_locator=lambda _: None
    )

    with pytest.raises(RuntimeError, match="Docker executable not found"):
        await runtime.preflight()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outputs", "message"),
    [
        ([_output(1, "daemon unavailable")], "Docker daemon is unavailable"),
        ([_output(), _output(1, "image missing")], "Docker image is unavailable"),
    ],
)
async def test_docker_preflight_reports_unavailable_resources(
    outputs: list[ExecutionOutput], message: str
) -> None:
    runner = RecordingRunner(outputs)
    runtime = DockerExperimentRuntime(
        "athena-experiment:test",
        command_runner=runner,
        executable_locator=lambda _: "docker",
    )

    with pytest.raises(RuntimeError, match=message):
        await runtime.preflight()
