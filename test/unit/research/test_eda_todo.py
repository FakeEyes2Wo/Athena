"""Unit tests for the EDA_TODO.md scheduler."""

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents import prepare_agent
from athena.research import eda_todo
from athena.research.eda_todo import run_eda_todos


class _FakeAgents:
    def __init__(self) -> None:
        self.spawned: list[dict] = []
        self.cancelled: list[tuple[str, str, str]] = []
        self.reaped: list[str] = []

    async def spawn(self, parent_id, agent_type, task, *, name=None):
        self.spawned.append(
            {"parent": parent_id, "type": agent_type, "task": task, "name": name}
        )
        return f"agent-{len(self.spawned)}", f"run-{len(self.spawned)}"

    async def reap(self, agent_id):
        self.reaped.append(agent_id)

    async def wait_run(self, run_id):
        return SimpleNamespace(run_id=run_id, status="COMPLETED", error=None)

    async def cancel_run(self, agent_id, run_id, reason):
        self.cancelled.append((agent_id, run_id, reason))


def _write_reports(workspace: Path, files: list[str]) -> None:
    for name in files:
        (workspace / name).write_text(
            "# Report\n\n- Concrete finding with enough detail for reuse. "
            "[eda:test:finding]\n",
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_run_eda_todos_marks_checkboxes_and_returns_no_failures(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path
    todo_file = workspace / "EDA_TODO.md"
    todo_file.write_text(
        "# EDA Todo\n"
        "\n"
        "## Stage 1: Overview (parallel: false)\n"
        "- [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md\n"
        "\n"
        "## Stage 2: Profiles (parallel: true)\n"
        "- [ ] 01 Quality -> EDA_REPORT_01_DATA_QUALITY.md\n"
        "- [ ] 02 Columns -> EDA_REPORT_02_COLUMNS.md\n",
        encoding="utf-8",
    )
    _write_reports(
        workspace,
        [
            "EDA_REPORT_00_OVERVIEW.md",
            "EDA_REPORT_01_DATA_QUALITY.md",
            "EDA_REPORT_02_COLUMNS.md",
        ],
    )

    agents = _FakeAgents()

    failed = await run_eda_todos(agents=agents, store=None, workspace=workspace)

    assert failed == []
    text = todo_file.read_text(encoding="utf-8")
    assert "- [x] 00 Overview" in text
    assert "- [x] 01 Quality" in text
    assert "- [x] 02 Columns" in text
    assert agents.spawned == []
    assert agents.reaped == []


@pytest.mark.asyncio
async def test_run_eda_todos_keeps_failed_todo_unchecked(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path
    todo_file = workspace / "EDA_TODO.md"
    todo_file.write_text(
        "# EDA Todo\n"
        "\n"
        "## Stage 1 (parallel: false)\n"
        "- [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md\n",
        encoding="utf-8",
    )
    agents = _FakeAgents()

    async def fake_wait(agents, run_id, publish):
        raise RuntimeError("worker failed")

    monkeypatch.setattr(eda_todo, "wait_run_events", fake_wait)

    # load_agent_result should not be reached on failure.
    async def fake_load(summary, store, result_type):
        raise AssertionError("should not load result after failure")

    monkeypatch.setattr(eda_todo, "load_agent_result", fake_load)

    failed = await run_eda_todos(
        agents=agents, store=None, workspace=workspace, retries=1
    )

    assert failed == ["00 Overview"]
    text = todo_file.read_text(encoding="utf-8")
    assert "- [ ] 00 Overview" in text
    assert agents.reaped == ["agent-1", "agent-2"]


@pytest.mark.asyncio
async def test_eda_worker_event_forwarder_is_awaitable(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "EDA_TODO.md").write_text(
        "## Stage 1 (parallel: false)\n" "- [ ] Overview -> EDA_REPORT.md\n",
        encoding="utf-8",
    )
    forwarded: list[tuple[str, str, str, dict | None]] = []

    async def project_event(agent_id, kind, ref, data=None):
        forwarded.append((agent_id, kind, ref, data))

    async def fake_wait(agents, run_id, publish):
        result = publish("agent/text_delta", "sha256:event", {"delta": "ready"})
        assert inspect.isawaitable(result)
        await result
        _write_reports(tmp_path, ["EDA_REPORT.md"])
        return SimpleNamespace(run_id=run_id, status="COMPLETED", error=None)

    async def fake_load(summary, store, result_type):
        raise AssertionError("a valid durable report should bypass JSON repair")

    monkeypatch.setattr(eda_todo, "wait_run_events", fake_wait)
    monkeypatch.setattr(eda_todo, "load_agent_result", fake_load)

    failed = await run_eda_todos(
        agents=_FakeAgents(),
        store=None,
        workspace=tmp_path,
        project_event=project_event,
    )

    assert failed == []
    assert forwarded == [
        ("agent-1", "agent/text_delta", "sha256:event", {"delta": "ready"})
    ]


@pytest.mark.asyncio
async def test_eda_worker_timeout_cancels_exact_run(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "EDA_TODO.md").write_text(
        "## Stage 1 (parallel: false)\n" "- [ ] Overview -> EDA_REPORT.md\n",
        encoding="utf-8",
    )
    agents = _FakeAgents()

    async def never_finishes(agents, run_id, publish):
        await asyncio.sleep(60)

    monkeypatch.setattr(eda_todo, "wait_run_events", never_finishes)

    failed = await run_eda_todos(
        agents=agents,
        store=None,
        workspace=tmp_path,
        timeout_seconds=0.01,
    )

    assert failed == ["Overview"]
    assert agents.cancelled == [("agent-1", "run-1", "eda_worker_timeout")]


def test_prepare_eda_agents_use_strict_cost_budgets(
    tmp_path: Path, monkeypatch
) -> None:
    registrations: list[dict] = []

    def capture_registration(registry, **kwargs):
        registrations.append(kwargs)

    monkeypatch.setattr(prepare_agent, "register_prompt_agent", capture_registration)
    prepare_agent.register_prepare_eda_agent(
        None,
        provider=object(),
        artifacts=None,
        workspace=tmp_path,
        runtime=None,
    )

    orchestrator, worker = registrations
    assert orchestrator["max_turns"] == 12
    assert worker["max_turns"] == 10
    assert orchestrator["max_tokens"] == 2048
    assert worker["max_tokens"] == 2048
