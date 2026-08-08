import pytest

from athena.core.agent_kernel.control import AgentControl, AgentHandle, AgentRun
from athena.core.agent_kernel.kernel import AgentKernel
from athena.core.agent_kernel.session import InMemoryResourcesFactory
from athena.core.agent_kernel.types import (
    AgentCommandError,
    AgentRunFailed,
    AgentRunInterrupted,
    AgentSpec,
)

from ._support import BlockingRunner, EchoRunner, JsonCodec, collect_events


def _control(**kw) -> AgentControl:
    kernel = AgentKernel(resources_factory=InMemoryResourcesFactory(), **kw)
    kernel._type_registry.register(
        "agent",
        lambda _aid, _cfg=None: AgentSpec(runner=EchoRunner(), codec=JsonCodec()),
    )
    return AgentControl(kernel)


def _type(control, runner=None, codec=None) -> str:
    """注册一个新 spec 到 control 的 type registry 并返回其 agent_type。"""
    spec = AgentSpec(runner=runner or EchoRunner(), codec=codec or JsonCodec())
    t = f"t{len(control.kernel._type_registry.types)}"
    control.kernel._type_registry.register(t, lambda _aid, _cfg=None: spec)
    return t


@pytest.mark.asyncio
async def test_create_root_returns_handle_and_run() -> None:
    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root("agent", {"q": 1})
    assert isinstance(handle, AgentHandle)
    assert isinstance(run, AgentRun)
    assert handle.agent_id.startswith("agent_")  # 不透明唯一 ID
    assert handle.name == "root"
    assert handle.path == ("root",)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_decodes_response() -> None:
    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root("agent", {"q": 1})
    response = await run.wait(timeout=2)
    assert response == {"echo": {"q": 1}}
    summary = await run.summary()
    assert summary.status.value == "completed"
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_spawn_followup_and_run_events_round_trip() -> None:
    control = _control()
    await control.kernel.start()
    parent, _ = await control.create_root("agent", {})
    child, child_run = await control.spawn(parent, "agent", {}, name="kid")
    assert child.path == ("root", "kid")
    follow = await control.followup(child, {"again": 2})
    resp = await follow.wait(timeout=2)
    assert resp == {"echo": {"again": 2}}
    events = await collect_events(follow.events(after_sequence=0), 2)
    assert [e.kind for e in events] == ["agent/step", "run_completed"]
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_handle_events_span_runs() -> None:
    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root("agent", {"q": 1})
    await run.wait(timeout=2)
    follow = await control.followup(handle, {"q": 2})
    await follow.wait(timeout=2)
    events = await collect_events(handle.events(after_sequence=0), 4)
    assert len(events) == 4  # 两轮各：agent/step + run_completed
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_raises_on_interrupt() -> None:
    control = _control()
    await control.kernel.start()
    runner = BlockingRunner()
    handle, run = await control.create_root(_type(control, runner), {})
    await runner.started.wait()
    await control.interrupt(handle, "stop")
    with pytest.raises(AgentRunInterrupted):
        await run.wait(timeout=2)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_raises_on_failed() -> None:
    class FailingRunner:
        async def run(self, request, *, session, emit) -> dict:
            raise RuntimeError("boom")

    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root(_type(control, FailingRunner()), {})
    with pytest.raises(AgentRunFailed):
        await run.wait(timeout=2)
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_list_agents_with_prefix() -> None:
    control = _control()
    await control.kernel.start()
    parent, _ = await control.create_root("agent", {})
    await control.spawn(parent, "agent", {}, name="a")
    await control.spawn(parent, "agent", {}, name="b")
    paths = {s.path for s in control.list_agents(path_prefix=("root",))}
    assert paths == {("root",), ("root", "a"), ("root", "b")}
    await control.kernel.aclose()


@pytest.mark.asyncio
async def test_cross_kernel_handle_rejected() -> None:
    control_a = _control()
    control_b = _control()
    await control_a.kernel.start()
    await control_b.kernel.start()
    handle, _ = await control_a.create_root("agent", {})
    with pytest.raises(AgentCommandError):
        await control_b.send_message(handle, "late")
    with pytest.raises(AgentCommandError):
        await control_b.spawn(handle, "agent", {})
    await control_a.kernel.aclose()
    await control_b.kernel.aclose()


@pytest.mark.asyncio
async def test_run_wait_decode_failure_is_sanitized() -> None:
    class LeakyCodec(JsonCodec):
        def decode_response(self, ref: str) -> object:
            raise RuntimeError("secret-provider-payload")

    control = _control()
    await control.kernel.start()
    handle, run = await control.create_root(
        _type(control, EchoRunner(), codec=LeakyCodec()), {}
    )
    with pytest.raises(AgentRunFailed) as raised:
        await run.wait(timeout=2)
    assert "secret" not in str(raised.value)  # 只暴露类型，防泄露（B11）
    await control.kernel.aclose()
