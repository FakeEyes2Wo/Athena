"""Codex CLI code-generation adapter."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from athena.code.backends import BackendUnavailableError
from athena.code.backends.base import (
    CodeBackend,
    generation_result,
    render_backend_prompt,
    snapshot_files,
)
from athena.code.types import ExecutionOutput, GenerationResult


@dataclass(frozen=True)
class _ProcessOutput:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., Awaitable[_ProcessOutput]]


async def _run_process(
    command: tuple[str, ...], *, cwd: str, timeout_s: float
) -> _ProcessOutput:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise BackendUnavailableError("codex CLI executable was not found") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_s
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise BackendUnavailableError(
            f"codex CLI timed out after {timeout_s:g}s"
        ) from exc
    return _ProcessOutput(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


class CodexBackend(CodeBackend):
    def __init__(
        self,
        *,
        runner: Runner | None = None,
        executable: str = "codex",
        model: str | None = None,
        timeout_s: float = 600,
    ) -> None:
        self._runner = runner or _run_process
        self._executable = executable
        self._model = model
        self._timeout_s = timeout_s

    @classmethod
    def from_config(cls, config: dict[str, object]) -> "CodexBackend":
        return cls(
            runner=config.get("runner"),  # type: ignore[arg-type]
            executable=str(config.get("executable", "codex")),
            model=(str(config["model"]) if config.get("model") else None),
            timeout_s=float(config.get("timeout_s", 600)),
        )

    async def generate(
        self,
        prompt: str,
        target_dir: str,
        previous_outputs: list[ExecutionOutput],
        history: list[dict],
    ) -> GenerationResult:
        before = snapshot_files(target_dir)
        command = [
            self._executable,
            "exec",
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--color",
            "never",
        ]
        if self._model is not None:
            command.extend(("--model", self._model))
        command.extend(
            ("-C", target_dir, render_backend_prompt(prompt, previous_outputs, history))
        )
        result = await self._runner(
            tuple(command), cwd=target_dir, timeout_s=self._timeout_s
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise BackendUnavailableError(
                f"codex CLI exited with code {result.returncode}: {detail}"
            )
        return generation_result(
            before,
            snapshot_files(target_dir),
            output=result.stdout.strip(),
        )
