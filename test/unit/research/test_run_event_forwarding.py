"""Agent journal 转发的游标语义：心跳重入不重放、失败不回退。"""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from athena.research.turns import common as agent_turn_common
from athena.research.turns.common import wait_run_with_heartbeat
from athena.research.supervisor.events import wait_run_events


@dataclass(frozen=True)
class _Event:
    """最小 AgentEvent 替身，只保留转发用到的字段。"""

    sequence: int
    kind: str
    event_ref: str
    data: dict | None = None


def _journal() -> list[_Event]:
    """一段典型 turn 的 journal：两条 agent 输出 + 终止事件。"""
    return [
        _Event(1, "agent/text_delta", "event:1", {"delta": "inspecting data"}),
        _Event(2, "agent/function_call", "event:2", {"tool": "shell_command"}),
        _Event(3, "turn_completed", "event:3"),
    ]


class _FakeAgents:
    """记录每次 ``run_events`` 的 after_sequence，并按游标续传 journal。

    ``stall_after`` 让第一轮转发到指定 sequence 后挂起，用来逼出一次心跳超时；
    ``stall_after=None`` 表示不挂起。``run_finished`` 表示 run 早已结束，此时
    ``wait_run`` 立即返回——它的完成与 journal 转发进度无关。
    """

    def __init__(
        self,
        events: list[_Event],
        stall_after: int | None = None,
        run_finished: bool = False,
    ) -> None:
        self._events = events
        self._stall_after = stall_after
        self.after_sequences: list[int] = []
        self._rounds = 0
        self._finished = asyncio.Event()
        if run_finished:
            self._finished.set()

    def run_events(self, _run_id, *, after_sequence: int = 0):
        self.after_sequences.append(after_sequence)
        first_round = self._rounds == 0
        self._rounds += 1

        async def stream():
            for event in self._events:
                if event.sequence <= after_sequence:
                    continue
                if (
                    first_round
                    and self._stall_after is not None
                    and event.sequence > self._stall_after
                ):
                    await asyncio.Event().wait()  # 一直挂到心跳超时取消本轮
                if event.kind == "turn_completed":
                    self._finished.set()
                yield event

        return stream()

    async def wait_run(self, _run_id):
        await self._finished.wait()
        return SimpleNamespace(error=None)


@pytest.mark.asyncio
async def test_heartbeat_reentry_resumes_after_last_forwarded_event(
    monkeypatch,
) -> None:
    """心跳超时后重入用上次游标续传，前端不会收到重复事件。"""
    monkeypatch.setattr(agent_turn_common, "TURN_HEARTBEAT_SECONDS", 0.05)
    agents = _FakeAgents(_journal(), stall_after=2)
    projected: list[tuple[str, str]] = []
    heartbeats: list[str] = []

    async def project_agent_event(_lane, kind, ref, _data):
        projected.append((kind, ref))

    async def publish_output(*, source, channel, text, plan):
        del source, channel, plan
        heartbeats.append(text)

    rt = SimpleNamespace(
        agents=agents,
        events=SimpleNamespace(project_agent_event=project_agent_event),
        publish_output=publish_output,
    )

    summary = await wait_run_with_heartbeat(
        rt, "run-1", agent_id="plan-worker", label="PLAN", plan="plan-1"
    )

    assert summary.error is None
    assert agents.after_sequences == [0, 2]
    assert projected == [
        ("agent/text_delta", "event:1"),
        ("agent/function_call", "event:2"),
        ("turn_completed", "event:3"),
    ]
    assert any("heartbeat" in text for text in heartbeats)


@pytest.mark.asyncio
async def test_forward_cursor_holds_and_error_propagates_when_publish_fails() -> None:
    """转发中途抛异常：异常照常上抛，游标停在最后一条成功转发的事件。"""
    agents = _FakeAgents(_journal(), run_finished=True)
    cursor = 0
    forwarded: list[str] = []

    def advance(sequence: int) -> None:
        nonlocal cursor
        cursor = max(cursor, sequence)

    async def publish(kind, ref, data):
        del data
        if kind == "agent/function_call":
            raise RuntimeError("projection failed")
        forwarded.append(ref)

    with pytest.raises(RuntimeError, match="projection failed"):
        await wait_run_events(
            agents, "run-1", publish, after_sequence=0, on_sequence=advance
        )

    assert cursor == 1
    assert forwarded == ["event:1"]
