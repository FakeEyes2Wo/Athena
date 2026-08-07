"""Compatibility wrappers for experiment execution."""

from pathlib import Path

from athena.code.execution import ExecutionRequest, LocalExperimentRuntime
from athena.code.types import ExecutionOutput


async def run_script(
    script_path: str, cwd: str, timeout_s: int = 600
) -> ExecutionOutput:
    """Run a Python script through the default local experiment runtime."""
    return await LocalExperimentRuntime().run(
        ExecutionRequest(
            entrypoint=script_path,
            cwd=Path(cwd),
            timeout_s=timeout_s,
        )
    )
