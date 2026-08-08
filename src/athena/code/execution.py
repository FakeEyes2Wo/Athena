"""Trusted experiment subprocess runtimes."""

import asyncio
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from athena.code.types import ExecutionOutput

_HOST_ENVIRONMENT_ALLOWLIST = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


@dataclass(frozen=True)
class ExecutionRequest:
    """Inputs required to execute one generated experiment."""

    entrypoint: str
    cwd: Path
    timeout_s: int
    environment: Mapping[str, str] = field(default_factory=dict)
    readonly_inputs: tuple[Path, ...] = ()


class ExperimentRuntime(Protocol):
    """Execution boundary used by the generated-code workflow."""

    async def preflight(self) -> None: ...

    async def run(self, request: ExecutionRequest) -> ExecutionOutput: ...


CommandRunner = Callable[..., Awaitable[ExecutionOutput]]
ExecutableLocator = Callable[[str], str | None]


class LocalExperimentRuntime:
    """Run experiments locally with a credential-free environment."""

    def __init__(self, base_environment: Mapping[str, str] | None = None) -> None:
        self._base_environment = dict(
            os.environ if base_environment is None else base_environment
        )

    async def preflight(self) -> None:
        return None

    async def run(self, request: ExecutionRequest) -> ExecutionOutput:
        cwd, entrypoint = _resolve_entrypoint(request)
        environment = _experiment_environment(
            self._base_environment, request.environment
        )
        return await _run_command(
            (sys.executable, str(entrypoint)),
            cwd=cwd,
            environment=environment,
            timeout_s=request.timeout_s,
        )


class DockerExperimentRuntime:
    """Run experiments in a network-disabled, resource-limited container."""

    def __init__(
        self,
        image: str,
        executable: str = "docker",
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 256,
        *,
        command_runner: CommandRunner | None = None,
        executable_locator: ExecutableLocator = shutil.which,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.image = image
        self.executable = executable
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self._command_runner = command_runner or _run_command
        self._executable_locator = executable_locator
        self._host_environment = _allowlisted_host_environment(
            os.environ if base_environment is None else base_environment
        )

    async def preflight(self) -> None:
        self._require_executable()
        daemon = await self._command_runner(
            (self.executable, "info"),
            cwd=None,
            environment=self._host_environment,
            timeout_s=30,
        )
        if daemon.returncode != 0:
            detail = daemon.stderr.strip() or "docker info failed"
            raise RuntimeError(f"Docker daemon is unavailable: {detail}")

        image = await self._command_runner(
            (self.executable, "image", "inspect", self.image),
            cwd=None,
            environment=self._host_environment,
            timeout_s=30,
        )
        if image.returncode != 0:
            detail = image.stderr.strip() or "docker image inspect failed"
            raise RuntimeError(f"Docker image is unavailable: {detail}")

    async def run(self, request: ExecutionRequest) -> ExecutionOutput:
        self._require_executable()
        cwd, entrypoint = _resolve_entrypoint(request)
        relative_entrypoint = entrypoint.relative_to(cwd).as_posix()
        command = self._build_command(request, cwd, relative_entrypoint)
        output = await self._command_runner(
            command,
            cwd=cwd,
            environment=self._host_environment,
            timeout_s=request.timeout_s,
        )
        return ExecutionOutput(
            stdout=output.stdout,
            stderr=output.stderr,
            returncode=output.returncode,
            files=_list_files(cwd),
        )

    def _require_executable(self) -> None:
        if self._executable_locator(self.executable) is None:
            raise RuntimeError(f"Docker executable not found: {self.executable}")

    def _build_command(
        self, request: ExecutionRequest, cwd: Path, entrypoint: str
    ) -> tuple[str, ...]:
        command = [
            self.executable,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            str(self.pids_limit),
            "--mount",
            f"type=bind,src={cwd},dst=/workspace",
        ]
        seen_targets: set[str] = set()
        for input_path in request.readonly_inputs:
            source = input_path.resolve()
            target = f"/workspace/.athena/inputs/{source.name}"
            if target in seen_targets:
                raise ValueError(f"duplicate read-only input name: {source.name}")
            seen_targets.add(target)
            command.extend(["--mount", f"type=bind,src={source},dst={target},readonly"])
        for name, value in sorted(request.environment.items()):
            if name.startswith("ATHENA_"):
                command.extend(("--env", f"{name}={value}"))
        command.extend(("--workdir", "/workspace", self.image, entrypoint))
        return tuple(command)


def _experiment_environment(
    base_environment: Mapping[str, str], requested: Mapping[str, str]
) -> dict[str, str]:
    environment = _allowlisted_host_environment(base_environment)
    environment.update(
        (name, value) for name, value in requested.items() if name.startswith("ATHENA_")
    )
    return environment


def _allowlisted_host_environment(
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    normalized = {name.upper(): value for name, value in base_environment.items()}
    return {
        name: normalized[name]
        for name in _HOST_ENVIRONMENT_ALLOWLIST
        if name in normalized
    }


def _resolve_entrypoint(request: ExecutionRequest) -> tuple[Path, Path]:
    cwd = request.cwd.resolve()
    candidate = Path(request.entrypoint)
    entrypoint = (candidate if candidate.is_absolute() else cwd / candidate).resolve()
    if not entrypoint.is_relative_to(cwd):
        raise ValueError("entrypoint must stay within cwd")
    return cwd, entrypoint


async def _run_command(
    command: tuple[str, ...],
    *,
    cwd: Path | None,
    environment: dict[str, str],
    timeout_s: int,
) -> ExecutionOutput:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=environment,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return ExecutionOutput(
            stdout="",
            stderr=f"TIMEOUT: process exceeded {timeout_s}s",
            returncode=-1,
        )
    except Exception as exc:
        return ExecutionOutput(stdout="", stderr=str(exc), returncode=-1)

    return ExecutionOutput(
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        returncode=process.returncode or 0,
        files=_list_files(cwd) if cwd is not None else [],
    )


def _list_files(cwd: Path) -> list[str]:
    return sorted(
        path.relative_to(cwd).as_posix() for path in cwd.rglob("*") if path.is_file()
    )
