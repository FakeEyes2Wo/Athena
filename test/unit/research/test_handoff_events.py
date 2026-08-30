"""handoff agent 的 journal 事件必须真的投影到事件总线。

``PhaseRunner._run_handoff_agent`` 的 publish 回调曾是同步函数、返回 ``None``，
而 ``forward_run_events`` 无条件 ``await publish(...)``：第一条 journal 事件就抛
``object NoneType can't be used in 'await' expression``，整段 EDA handoff 退化成
fallback 文件。同一处还有第二个错——``project_agent_event`` 是协程却没被 await，
即便不抛错事件也一条都投影不出去。两次真机运行稳定复现。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.phase_runner import PhaseRunner


class _Agents:
    """只够跑一次 handoff 的 AgentRuntime 替身。"""

    def __init__(self) -> None:
        self.events = [
            SimpleNamespace(
                kind="agent/text_delta",
                event_ref="ref-1",
                data={"delta": "写 EDA_INDEX"},
                sequence=1,
            ),
            SimpleNamespace(
                kind="turn_completed", event_ref="ref-2", data={}, sequence=2
            ),
        ]

    def has_agent(self, agent_id: str) -> bool:
        del agent_id
        return False

    async def create_root(self, agent_type, request, *, agent_id, name):
        del agent_type, request, name
        return agent_id, "run-1"

    async def run_events(self, run_id, after_sequence=0):
        del run_id
        for event in self.events:
            if event.sequence > after_sequence:
                yield event

    async def wait_run(self, run_id):
        del run_id
        return SimpleNamespace(status="COMPLETED", response_ref=None, error=None)


@pytest.mark.asyncio
async def test_handoff_agent_projects_its_journal_events(tmp_path: Path) -> None:
    """publish 回调必须可 await，且真的把事件投影出去。"""
    projected: list[tuple[str, str]] = []

    async def project_agent_event(agent_id, kind, ref, data=None):
        del ref, data
        projected.append((agent_id, kind))

    runtime = SimpleNamespace(
        agents=_Agents(),
        events=SimpleNamespace(project_agent_event=project_agent_event),
        store=SimpleNamespace(),
    )
    (tmp_path / "EDA_INDEX.md").write_text("# index\n", encoding="utf-8")

    text = await PhaseRunner(runtime)._run_handoff_agent(
        agent_id="prepare_eda",
        agent_type="prepare_eda",
        workspace=str(tmp_path),
        output_file="EDA_INDEX.md",
        content="Finalize EDA",
    )

    assert text == "# index\n"
    assert ("prepare_eda", "agent/text_delta") in projected
    assert ("prepare_eda", "turn_completed") in projected


@pytest.mark.asyncio
async def test_handoff_agent_survives_a_runtime_without_an_event_bus(
    tmp_path: Path,
) -> None:
    """没有事件总线时（测试替身 runtime）照常完成，不投影即可。"""
    runtime = SimpleNamespace(agents=_Agents(), events=None, store=SimpleNamespace())
    (tmp_path / "EDA_INDEX.md").write_text("# index\n", encoding="utf-8")

    text = await PhaseRunner(runtime)._run_handoff_agent(
        agent_id="prepare_eda",
        agent_type="prepare_eda",
        workspace=str(tmp_path),
        output_file="EDA_INDEX.md",
        content="Finalize EDA",
    )

    assert text == "# index\n"


@pytest.mark.asyncio
async def test_eda_todo_worker_projects_its_journal_events(tmp_path: Path) -> None:
    """EDA todo worker 的 publish 回调有同一个毛病：同步、返回 None。

    ``_run_one`` 里的 publish 被 ``wait_run_events`` 无条件 await，于是每个 worker
    第一条事件就抛 TypeError；异常被 ``except Exception`` 吞掉并重试，重试同样失败，
    整张 EDA todo 表一条也完不成，最后全靠占位文件填坑。
    """
    from athena.research.eda_todo import run_eda_todos

    projected: list[tuple[str, str]] = []

    def project_event(agent_id, kind, ref, data=None):
        async def _project():
            projected.append((agent_id, kind))

        del ref, data
        return _project()

    class _TodoAgents(_Agents):
        async def spawn(self, parent_id, agent_type, task, *, name):
            del parent_id, agent_type, task, name
            # worker 真的把报告写出来，这样只剩 publish 这一个失败点。
            (tmp_path / "EDA_REPORT_00_OVERVIEW.md").write_text(
                "# overview\n", encoding="utf-8"
            )
            return "eda-worker", "run-1"

        async def reap(self, agent_id):
            del agent_id

        async def wait_run(self, run_id):
            del run_id
            return SimpleNamespace(
                status="completed",
                response_ref=json.dumps({"result_ref": "handoff-ref"}),
                error=None,
            )

    class _Store:
        async def get_text(self, ref):
            del ref
            return json.dumps(
                {
                    "summary": "overview done",
                    "handoff_file": "EDA_REPORT_00_OVERVIEW.md",
                }
            )

    (tmp_path / "EDA_TODO.md").write_text(
        "## Stage 1 (parallel: false)\n"
        "- [ ] 00 Overview -> EDA_REPORT_00_OVERVIEW.md\n",
        encoding="utf-8",
    )

    failed = await run_eda_todos(
        agents=_TodoAgents(),
        store=_Store(),
        workspace=tmp_path,
        retries=0,
        project_event=project_event,
    )

    assert failed == []
    assert ("eda-worker", "agent/text_delta") in projected
