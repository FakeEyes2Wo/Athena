"""handoff agent 的事件回调必须是可 await 的。

真机（2026-08-29，qwen3.7-plus 跑 TESS 恒星耀发任务）：EDA 的九份
``EDA_REPORT_*.md`` 全部生成完毕，然后 handoff 在**第一个 agent 事件**上炸掉：

    supervisor> EDA handoff failed (object NoneType can't be used in 'await'
                expression); writing fallback EDA files.
    supervisor> PREPARE: 因 EDA 失败跳过 BASELINE_DESIGN，prepare 将基于任务原文降级。

``forward_run_events`` 对每个事件做 ``await publish(...)``，而
``_run_handoff_agent`` 里的 ``publish`` 是个普通 ``def``、返回 None。两处后果：
被丢掉的协程从来没被 await（事件根本没投递），以及 ``await None`` 直接抛
TypeError。于是**每一次运行**的 EDA 产出都被丢弃，PREPARE 降级成只看任务原文——
报告就躺在磁盘上，没有任何人读。
"""

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.phase_runner import PhaseRunner


class _Events:
    """Stand-in for the runtime event bus; ``project_agent_event`` is async."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    async def project_agent_event(self, agent_id, kind, ref, data=None) -> None:
        self.seen.append((agent_id, kind))


class _Agents:
    """Emits one journal event then a terminal one, awaiting publish each time."""

    def __init__(self) -> None:
        self.reaped: list[str] = []

    def has_agent(self, agent_id: str) -> bool:
        return False

    async def create_root(self, agent_type, request, *, agent_id, name):
        return agent_id, "run-1"

    async def wait_run(self, run_id):
        return SimpleNamespace(
            status="COMPLETED", response_ref=None, result_ref=None, error=None
        )

    async def run_events(self, run_id, after_sequence=0):
        for kind in ("agent/text_delta", "run/completed"):
            yield SimpleNamespace(kind=kind, event_ref="ref", data={})

    async def reap(self, agent_id: str) -> None:
        self.reaped.append(agent_id)


def _runner(tmp_path: Path, events: _Events, agents: _Agents) -> PhaseRunner:
    runtime = SimpleNamespace(agents=agents, store=object(), events=events)
    return PhaseRunner(runtime)


def test_the_publish_callback_is_a_coroutine_function(tmp_path: Path) -> None:
    """``forward_run_events`` 对它做 await；普通函数返回的 None 无法 await。"""
    source = inspect.getsource(PhaseRunner._run_handoff_agent)
    assert "async def publish" in source


@pytest.mark.asyncio
async def test_handoff_forwards_events_and_returns_the_output_file(
    tmp_path: Path,
) -> None:
    events, agents = _Events(), _Agents()
    (tmp_path / "EDA_INDEX.md").write_text("# index\n", encoding="utf-8")

    text = await _runner(tmp_path, events, agents)._run_handoff_agent(
        agent_id="prepare_eda",
        agent_type="prepare_eda",
        workspace=str(tmp_path),
        output_file="EDA_INDEX.md",
        content="finalize",
        reap_after=True,
    )

    assert text == "# index\n"
    # 事件真的被投递了，而不是把协程丢掉
    assert events.seen == [
        ("prepare_eda", "agent/text_delta"),
        ("prepare_eda", "run/completed"),
    ]
    assert agents.reaped == ["prepare_eda"]


@pytest.mark.asyncio
async def test_a_runtime_without_an_event_bus_still_works(tmp_path: Path) -> None:
    agents = _Agents()
    (tmp_path / "EDA_INDEX.md").write_text("# index\n", encoding="utf-8")
    runtime = SimpleNamespace(agents=agents, store=object(), events=None)

    text = await PhaseRunner(runtime)._run_handoff_agent(
        agent_id="prepare_eda",
        agent_type="prepare_eda",
        workspace=str(tmp_path),
        output_file="EDA_INDEX.md",
        content="finalize",
    )

    assert text == "# index\n"


def test_project_agent_event_is_async() -> None:
    """这条用例钉住上面那条断言的前提：事件投递本身是协程。"""
    from athena.research.runtime_events import RuntimeEvents

    assert asyncio.iscoroutinefunction(RuntimeEvents.project_agent_event)
