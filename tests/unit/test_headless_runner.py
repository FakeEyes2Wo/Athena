"""The headless entry must preserve run ownership and report terminal failure."""

import asyncio
from types import SimpleNamespace

import pytest

from athena.core.project_lock import project_lock
from scripts import run_headless


@pytest.fixture
def runtime_factory(monkeypatch):
    instances = []

    class Runtime:
        phase = "COMPLETED"
        status = "IDLE"
        error = None

        def __init__(self, **kwargs):
            self.options = kwargs
            self.state = SimpleNamespace(phase=self.phase, status=self.status)
            self.closed = False
            instances.append(self)

        def subscribe(self, callback):
            return "subscription"

        async def start(self):
            if self.error is not None:
                raise self.error
            task = asyncio.get_running_loop().create_future()
            task.set_result(None)
            return task

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(run_headless, "ResearchRuntime", Runtime)
    return Runtime, instances


@pytest.mark.parametrize(
    "phase,status,expected",
    [("COMPLETED", "IDLE", 0), ("SEARCH", "FAILED", 1), ("SEARCH", "WAITING", 1)],
)
async def test_exit_status_requires_completed_research(
    tmp_path, runtime_factory, phase, status, expected
):
    runtime, instances = runtime_factory
    runtime.phase, runtime.status = phase, status
    args = run_headless._parse(["--project", str(tmp_path), "--task", "demo"])
    assert await run_headless._run(args) == expected
    assert instances[0].closed
    assert instances[0].options["research"].task.auto_confirm


async def test_cancellation_always_closes_runtime(tmp_path, runtime_factory):
    runtime, instances = runtime_factory
    runtime.error = asyncio.CancelledError()
    args = run_headless._parse(["--project", str(tmp_path), "--task", "demo"])
    with pytest.raises(asyncio.CancelledError):
        await run_headless._run(args)
    assert instances[0].closed


def test_active_project_blocks_headless_start(tmp_path, runtime_factory):
    _runtime, instances = runtime_factory
    with project_lock(tmp_path / ".athena"):
        assert run_headless.main(["--project", str(tmp_path), "--task", "demo"]) == 2
    assert instances == []


def test_negative_search_budget_is_rejected(tmp_path, runtime_factory):
    with pytest.raises(SystemExit) as exc:
        run_headless._parse(
            ["--project", str(tmp_path), "--task", "demo", "--search-limit", "-1"]
        )
    assert exc.value.code == 2
