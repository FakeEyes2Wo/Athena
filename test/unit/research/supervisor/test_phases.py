"""阶段机总入口的失败可观测性。

``PhaseMachine.start`` 是 PREPARE/SEARCH/VALIDATE 的唯一入口，也是 fire-and-forget
的 supervisor task 唯一的兜底。它只把 ``str(exc)`` 发给前端，不记 traceback 的话，
一次真机失败（实测 ``not enough values to unpack (expected 2, got 0)``）在磁盘上
留不下任何出处，只能靠猜。
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from athena.research.supervisor.phases import PhaseMachine


def _machine(recover):
    """一台只够跑 ``start()`` 兜底分支的 PhaseMachine。"""
    published: list[dict] = []

    async def publish(kind, payload):
        published.append({"kind": kind, **payload})

    async def publish_state():
        return None

    state = SimpleNamespace(phase="SEARCH", status="RUNNING")
    machine = PhaseMachine(
        owner=SimpleNamespace(state=state, recover=recover),
        deps=SimpleNamespace(publish=publish),
        run=SimpleNamespace(set_stopped=lambda _stopped: None),
        plans=SimpleNamespace(_save_state=lambda: None, _publish_state=publish_state),
        search=SimpleNamespace(),
    )
    return machine, state, published


@pytest.mark.asyncio
async def test_start_logs_the_traceback_of_a_failed_phase(caplog) -> None:
    """阶段失败必须留下 traceback，否则出处不可恢复。"""

    async def recover():
        raise ValueError("not enough values to unpack (expected 2, got 0)")

    machine, state, published = _machine(recover)

    with caplog.at_level(logging.ERROR, logger="athena.research.supervisor.phases"):
        await machine.start()

    assert state.status == "FAILED"
    assert any(
        record.exc_info for record in caplog.records
    ), "阶段机兜底没有记录 traceback"
    assert "ValueError" in caplog.text


@pytest.mark.asyncio
async def test_start_still_reports_the_reason_to_the_frontend() -> None:
    """记日志不能取代回传：前端仍然要看到失败原因。"""

    async def recover():
        raise ValueError("boom")

    machine, state, published = _machine(recover)

    await machine.start()

    assert state.status == "FAILED"
    errors = [p for p in published if p.get("channel") == "error"]
    assert errors and errors[0]["text"] == "research failed: boom"


@pytest.mark.asyncio
async def test_start_does_not_swallow_cancellation() -> None:
    """取消（暂停/停止）必须原样上抛，不能被当成阶段失败。"""

    async def recover():
        raise asyncio.CancelledError

    machine, state, _published = _machine(recover)

    with pytest.raises(asyncio.CancelledError):
        await machine.start()
    assert state.status == "RUNNING"
