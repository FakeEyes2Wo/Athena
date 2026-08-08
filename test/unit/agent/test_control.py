import pytest

from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.control import AgentControl, AgentHandle, AgentRun
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import (
    AgentCommandError,
    AgentRunFailed,
    AgentRunInterrupted,
    AgentSpec,
)

from ._support import (
    BlockingRunner,
    EchoRunner,
    JsonCodec,
    _TERMINAL_EVENT_KINDS,
    collect_until_terminal,
)


def _control(tmp_path) -> AgentControl:
    registry = AgentTypeRegistry()
    registry.register(
        "agent",
        lambda _aid, _cfg=None: AgentSpec(runner=EchoRunner(), codec=JsonCodec()),
    )
    rt = AgentRuntime(type_registry=registry, project_root=tmp_path)
    rt.start()
    return AgentControl(rt)


def _type(control, runner=None, codec=None) -> str:
    """注册一个新 spec 到 runtime 的 type registry 并返回其 agent_type。"""
    spec = AgentSpec(runner=runner or EchoRunner(), codec=codec or JsonCodec())
    t = f"t{len(control.kernel._registry.types)}"
    control.kernel._registry.register(t, lambda _aid, _cfg=None: spec)
    return t


@pytest.mark.asyncio
async def test_create_root_returns_handle_and_run(tmp_path) -> None:
    control = _control(tmp_path)
    handle, run = await control.create_root("agent", {"q": 1})
    assert isinstance(handle, AgentHandle)
    assert isinstance(run, AgentRun)
    assert handle.agent_id  # 不透明唯一 ID
    assert handle.name == "root"
    assert handle.path == ("root",)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_decodes_response(tmp_path) -> None:
    control = _control(tmp_path)
    handle, run = await control.create_root("agent", {"q": 1})
    response = await run.wait(timeout=5)
    assert response == {"echo": {"q": 1}}
    summary = await run.summary()
    assert summary.status.value == "completed"
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_followup_and_run_events_round_trip(tmp_path) -> None:
    control = _control(tmp_path)
    parent, _ = await control.create_root("agent", {})
    child, child_run = await control.spawn(parent, "agent", {}, name="kid")
    assert child.path == ("root", "kid")
    follow = await control.followup(child, {"again": 2})
    resp = await follow.wait(timeout=5)
    assert resp == {"echo": {"again": 2}}
    events = await collect_until_terminal(follow.events(after_sequence=0))
    assert events[-1].kind == "turn_completed"
    assert any(e.kind == "agent/step" for e in events)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_handle_events_span_runs(tmp_path) -> None:
    control = _control(tmp_path)
    handle, run = await control.create_root("agent", {"q": 1})
    await run.wait(timeout=5)
    follow = await control.followup(handle, {"q": 2})
    await follow.wait(timeout=5)
    # session journal 跨 turn 追加;collect_until_terminal 只到首个终态,
    # 这里收集到第二个 turn_completed 以覆盖两轮事件(RunSession 视图跨 run)。
    events: list = []
    async for e in handle.events(after_sequence=0):
        events.append(e)
        if sum(1 for x in events if x.kind in _TERMINAL_EVENT_KINDS) >= 2:
            break
    assert events[-1].kind == "turn_completed"
    assert any(e.kind == "agent/step" for e in events)
    assert len({e.run_id for e in events}) == 2  # 两轮事件同属一个 session journal
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_raises_on_interrupt(tmp_path) -> None:
    control = _control(tmp_path)
    runner = BlockingRunner()
    handle, run = await control.create_root(_type(control, runner), {})
    await runner.started.wait()
    await control.interrupt(handle, "stop")
    with pytest.raises(AgentRunInterrupted):
        await run.wait(timeout=5)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_raises_on_failed(tmp_path) -> None:
    class FailingRunner:
        async def run(self, request, *, session, emit) -> dict:
            raise RuntimeError("boom")

    control = _control(tmp_path)
    handle, run = await control.create_root(_type(control, FailingRunner()), {})
    with pytest.raises(AgentRunFailed):
        await run.wait(timeout=5)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_list_agents_with_prefix(tmp_path) -> None:
    control = _control(tmp_path)
    parent, _ = await control.create_root("agent", {})
    await control.spawn(parent, "agent", {}, name="a")
    await control.spawn(parent, "agent", {}, name="b")
    paths = {s.path for s in control.list_agents(path_prefix=("root",))}
    assert paths == {("root",), ("root", "a"), ("root", "b")}
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_cross_kernel_handle_rejected(tmp_path) -> None:
    control_a = _control(tmp_path)
    control_b = _control(tmp_path)
    handle, _ = await control_a.create_root("agent", {})
    with pytest.raises(AgentCommandError):
        await control_b.send_message(handle, "late")
    with pytest.raises(AgentCommandError):
        await control_b.spawn(handle, "agent", {})
    await control_a.kernel.aclose()
    await control_b.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_decode_failure_is_sanitized(tmp_path) -> None:
    class LeakyCodec(JsonCodec):
        def decode_response(self, ref: str) -> object:
            raise RuntimeError("secret-provider-payload")

    control = _control(tmp_path)
    handle, run = await control.create_root(
        _type(control, EchoRunner(), codec=LeakyCodec()), {}
    )
    with pytest.raises(AgentRunFailed) as raised:
        await run.wait(timeout=5)
    assert "secret" not in str(raised.value)  # 只暴露类型，防泄露（B11）
    await control.kernel.aclose()
