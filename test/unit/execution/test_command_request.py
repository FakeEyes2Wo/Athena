"""CommandRequest / per-run ``predict_features`` execution-interface tests.

The immutable request carries command/argv/timeout/workdir/emit.  The mutable
global predict-feature chain is gone: each command's
``ATHENA_PREDICT_FEATURES`` is derived from the frozen ``ExecutionContext`` for
that one command only, so sequential runs cannot leak a target into the next.

The sandbox forbids named-pipe child processes and listing pytest-created temp
roots, so these tests use a recording backend plus ``EnvironmentManager``'s
per-call env builder.  That still covers the interface's contract: every run
materialises the context's predict target onto that run's request, and the
local env builder injects it exactly when a target is present.
"""

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.execution import (
    CommandRequest,
    ExecutionContext,
    ExecutionRuntime,
)
from athena.execution.remote.ssh import SshBackend, SshHost
from athena.execution.runtime import CommandResult, EnvironmentManager


class _RecordingBackend:
    """Minimal backend that only records the per-run request."""

    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []
        self.workspaces: list[Path] = []

    @property
    def name(self) -> str:
        return "recording"

    def describe(self, workspace_root) -> str:
        return "Runtime:\n- OS: recording"

    def env_ref(self, name: str) -> str:
        return f"${name}"

    def ensure_environment(self) -> None:
        pass

    async def run(
        self, *, workspace_root: Path, request: CommandRequest
    ) -> CommandResult:
        self.requests.append(request)
        self.workspaces.append(workspace_root)
        return CommandResult(ok=True, stdout="", stderr="", exit_code=0)

    async def collect_outputs(self, subdirs) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _work_dir() -> Path:
    path = Path.cwd() / f".test-command-request-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _feature(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("row,feature\n", encoding="utf-8")
    return path


def _context(
    work_root: Path,
    *,
    predict_features: Path | None = None,
) -> ExecutionContext:
    return ExecutionContext(
        project_root=work_root,
        workspace_root=work_root,
        environment_root=work_root,
        predict_features=predict_features,
    )


def test_command_request_defaults_preserve_old_behaviour() -> None:
    request = CommandRequest(command="echo hi")

    assert request.command == "echo hi"
    assert request.argv is None
    assert request.timeout_s == 120
    assert request.workdir is None
    assert request.emit is None
    assert request.predict_features is None
    assert request.evaluation_split is None


def test_environment_manager_injects_predict_features_exactly_when_requested() -> None:
    work_dir = _work_dir()
    target = _feature(work_dir / "data_split" / "search_features.csv")
    try:
        manager = EnvironmentManager(project_root=work_dir, environment_root=work_dir)

        with_target = manager.build_env(work_dir, predict_features=target)
        assert with_target["ATHENA_PREDICT_FEATURES"] == str(target)

        without_target = manager.build_env(work_dir)
        assert "ATHENA_PREDICT_FEATURES" not in without_target
    finally:
        _cleanup(work_dir)


def test_environment_manager_injects_evaluation_split_per_request() -> None:
    work_dir = _work_dir()
    try:
        manager = EnvironmentManager(project_root=work_dir, environment_root=work_dir)

        search = manager.build_env(work_dir, evaluation_split="search")
        final = manager.build_env(work_dir, evaluation_split="final")
        plain = manager.build_env(work_dir)

        assert search["ATHENA_EVALUATION_SPLIT"] == "search"
        assert final["ATHENA_EVALUATION_SPLIT"] == "final"
        assert "ATHENA_EVALUATION_SPLIT" not in plain
    finally:
        _cleanup(work_dir)


@pytest.mark.asyncio
async def test_predict_features_is_materialised_onto_the_per_run_request() -> None:
    work_dir = _work_dir()
    target = _feature(work_dir / "data_split" / "search_features.csv")
    try:
        backend = _RecordingBackend()
        runtime = ExecutionRuntime(
            project_root=work_dir, environment_root=work_dir, backend=backend
        )

        await runtime.run(
            _context(work_dir, predict_features=target),
            CommandRequest(command="python train.py"),
        )
        await runtime.run(_context(work_dir), CommandRequest(command="python train.py"))

        assert [request.predict_features for request in backend.requests] == [
            target,
            None,
        ]
    finally:
        _cleanup(work_dir)


@pytest.mark.asyncio
async def test_no_leakage_between_two_sequential_runs_with_different_targets() -> None:
    work_dir = _work_dir()
    first = _feature(work_dir / "split" / "first.csv")
    second = _feature(work_dir / "split" / "second.csv")
    try:
        backend = _RecordingBackend()
        runtime = ExecutionRuntime(
            project_root=work_dir, environment_root=work_dir, backend=backend
        )

        await runtime.run(
            _context(work_dir, predict_features=first),
            CommandRequest(command="run one"),
        )
        await runtime.run(
            _context(work_dir, predict_features=second),
            CommandRequest(command="run two"),
        )
        await runtime.run(_context(work_dir), CommandRequest(command="run three"))

        assert [request.predict_features for request in backend.requests] == [
            first,
            second,
            None,
        ]
    finally:
        _cleanup(work_dir)


@pytest.mark.asyncio
async def test_ssh_backend_rejects_unsupported_predict_features_with_clear_error() -> (
    None
):
    target = Path.cwd() / f".test-ssh-predict-{uuid.uuid4().hex}" / "final.csv"
    backend = SshBackend(
        SshHost(name="gpu-01", alias="gpu01.lab"),
        channel=SimpleNamespace(
            ready={"shell": "bash", "path": "/usr/bin", "packages": {}}
        ),
        remote_workspace="/scratch/athena/lease/workspace",
    )
    request = CommandRequest(command="python train.py", predict_features=target)

    with pytest.raises(NotImplementedError) as caught:
        # This backend cannot support the control node's absolute split path on
        # the remote host; it must reject through the request, not a protocol
        # setter that can be forgotten.
        await backend.run(workspace_root=Path.cwd(), request=request)

    message = str(caught.value)
    assert "ATHENA_PREDICT_FEATURES" in message
    assert "ssh backend" in message
    assert "does not resolve on the remote host" in message
