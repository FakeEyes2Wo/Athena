"""Unit tests for the EDA_TODO.md scheduler."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents.ideator_agent import HandoffResult
from athena.research.prepare import eda as eda_todo
from athena.research.prepare.eda import run_eda_todos


class _FakeAgents:
    def __init__(self) -> None:
        self.spawned: list[dict] = []
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


def _write_reports(workspace: Path, files: list[str]) -> None:
    for name in files:
        (workspace / name).write_text("# report\n", encoding="utf-8")


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

    async def fake_wait(agents, run_id, publish):
        return SimpleNamespace(run_id=run_id, status="COMPLETED", error=None)

    async def fake_load(summary, store, result_type):
        return HandoffResult(summary="ok", handoff_file="EDA_REPORT.md")

    monkeypatch.setattr(eda_todo, "wait_run_events", fake_wait)
    monkeypatch.setattr(eda_todo, "load_agent_result", fake_load)

    failed = await run_eda_todos(agents=agents, store=None, workspace=workspace)

    assert failed == []
    text = todo_file.read_text(encoding="utf-8")
    assert "- [x] 00 Overview" in text
    assert "- [x] 01 Quality" in text
    assert "- [x] 02 Columns" in text
    assert len(agents.spawned) == 3
    assert agents.spawned[0]["type"] == "eda_worker"
    first_task = agents.spawned[0]["task"]
    assert first_task["output_file"] == "EDA_REPORT_00_OVERVIEW.md"
    content = first_task["content"]
    assert "EDA_REPORT_00_OVERVIEW.md" in content
    assert "at most 8 tool calls" in content
    assert "Use only installed dependencies" in content
    assert "do not install packages with pip, conda, or uv" in content
    assert "Keep the report focused" in content
    assert "do not exhaustively enumerate the dataset" in content
    assert agents.reaped == [f"agent-{i}" for i in range(1, 4)]


@pytest.mark.asyncio
async def test_run_eda_todos_skips_orchestrator_index_and_handoff_todo(
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
        "## Stage 4: Final (parallel: false)\n"
        "- [ ] 07 Index & Handoff -> EDA_INDEX.md + EDA_HANDOFF.md\n",
        encoding="utf-8",
    )
    _write_reports(workspace, ["EDA_REPORT_00_OVERVIEW.md"])

    agents = _FakeAgents()

    async def fake_wait(agents, run_id, publish):
        return SimpleNamespace(run_id=run_id, status="COMPLETED", error=None)

    async def fake_load(summary, store, result_type):
        return HandoffResult(summary="ok", handoff_file="EDA_REPORT.md")

    monkeypatch.setattr(eda_todo, "wait_run_events", fake_wait)
    monkeypatch.setattr(eda_todo, "load_agent_result", fake_load)

    failed = await run_eda_todos(agents=agents, store=None, workspace=workspace)

    # The Index & Handoff step belongs to the orchestrator finalize turn; it
    # must not be scheduled as an EDA worker (which is forbidden to write it).
    assert failed == []
    assert len(agents.spawned) == 1
    assert agents.spawned[0]["task"]["output_file"] == "EDA_REPORT_00_OVERVIEW.md"
    text = todo_file.read_text(encoding="utf-8")
    assert "- [x] 00 Overview" in text
    assert "- [ ] 07 Index & Handoff" in text


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
    _write_reports(workspace, ["EDA_REPORT_00_OVERVIEW.md"])

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
async def test_timed_out_todo_fails_once_and_reaps_worker(
    tmp_path: Path, monkeypatch
) -> None:
    """A hung worker must consume one attempt, not three unbounded retries."""
    todo_file = tmp_path / "EDA_TODO.md"
    todo_file.write_text(
        "## Stage 1 (parallel: false)\n"
        "- [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md\n",
        encoding="utf-8",
    )
    agents = _FakeAgents()

    async def never_finishes(_agents, _run_id, _publish):
        await asyncio.Event().wait()

    monkeypatch.setattr(eda_todo, "wait_run_events", never_finishes)
    monkeypatch.setattr(eda_todo, "AGENT_TURN_TIMEOUT_SECONDS", 0.01)

    failed = await run_eda_todos(
        agents=agents,
        store=None,
        workspace=tmp_path,
        retries=2,
    )

    assert failed == ["00 Overview"]
    assert len(agents.spawned) == 1
    assert agents.reaped == ["agent-1"]
    assert "- [ ] 00 Overview" in todo_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_report_already_on_disk_is_not_regenerated(tmp_path) -> None:
    """EDA 必须能续跑。

    ``EDA_TODO.md`` 的复选框是名义上的续跑标记，但每次 PREPARE 重启时
    prepare_eda agent 会**重新生成**这份 todo 列表，把所有复选框清回未完成。
    于是续跑会对着磁盘上已经写好的报告从头再跑一遍——真机（2026-08-29）上
    因此反复重入 EDA。持久的信号是产物本身，不是复选框。
    """

    class Agents:
        def __init__(self) -> None:
            self.spawned = 0

        async def spawn(self, *a, **k):
            self.spawned += 1
            return "w1", "run-1"

        async def reap(self, agent_id):
            pass

    workspace = tmp_path
    (workspace / "EDA_REPORT_00_OVERVIEW.md").write_text("x" * 500, encoding="utf-8")
    (workspace / "EDA_TODO.md").write_text(
        "## Stage (parallel: true)\n- [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md\n",
        encoding="utf-8",
    )
    agents = Agents()

    failed = await run_eda_todos(
        agents=agents, store=object(), workspace=workspace, project_event=None
    )

    assert failed == []
    assert agents.spawned == 0, "已经写好的报告不该再花一个 worker"
    assert "- [x]" in (workspace / "EDA_TODO.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_stub_sized_report_is_still_regenerated(tmp_path) -> None:
    """占位文件（EDA 失败时写的那种）必须重跑，不能当成已完成。"""

    class Agents:
        def __init__(self) -> None:
            self.spawned = 0

        async def spawn(self, *a, **k):
            self.spawned += 1
            raise RuntimeError("worker unavailable")

        async def reap(self, agent_id):
            pass

    workspace = tmp_path
    (workspace / "EDA_REPORT_00_OVERVIEW.md").write_text("stub\n", encoding="utf-8")
    (workspace / "EDA_TODO.md").write_text(
        "## Stage (parallel: true)\n- [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md\n",
        encoding="utf-8",
    )
    agents = Agents()

    failed = await run_eda_todos(
        agents=agents, store=object(), workspace=workspace, project_event=None
    )

    assert agents.spawned > 0
    assert failed
