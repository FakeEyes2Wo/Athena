import asyncio
import os
import stat
from pathlib import Path

import pytest

from athena.research.supervisor.state import ResearchState
from gui_gateway.handler import GuiRequestHandler, _session_state_root
from gui_gateway.human import HumanRequestBroker


class RecordingRuntime:
    def __init__(self) -> None:
        self.started = False
        self.messages: list[str] = []
        self.tree_path = Path("/tmp/athena-runtime")
        self.project_root = "/tmp"
        # 拆卸顺序断言用：suspend 必须发生在 aclose 之前。
        self.calls: list[str] = []
        self.suspend_error: Exception | None = None

    async def start(self) -> None:
        self.started = True

    async def message(self, text: str) -> str:
        self.messages.append(text)
        return "accepted"

    async def suspend(self) -> str:
        """替身版 ``ResearchRuntime.suspend``：真实落盘，状态守卫由真 Supervisor 负责。"""
        self.calls.append("suspend")
        if self.suspend_error is not None:
            raise self.suspend_error
        state = getattr(self, "state", None)
        if state is None or state.status != "RUNNING":
            return getattr(state, "status", "IDLE")
        state.status = "WAITING"
        state.save(self._state_path)
        return state.status

    async def aclose(self) -> None:
        self.calls.append("aclose")
        self.started = False

    async def start_validation(self) -> str:
        return "COMPLETED"

    def settings(self) -> dict[str, str]:
        return {"project_root": self.project_root}

    def replay_output_events(self) -> list[dict]:
        return []


def _handler_at(tmp_path: Path, factory=None) -> GuiRequestHandler:
    runtime = RecordingRuntime()
    runtime.project_root = str(tmp_path)
    return GuiRequestHandler(runtime, factory)


def _running_state(phase: str = "SEARCH") -> ResearchState:
    """一个「进行中」的研究状态，用于伪造断点续传场景。"""
    return ResearchState(status="RUNNING", phase=phase, search_limit=10, concurrency=4)


@pytest.mark.asyncio
async def test_handler_exposes_only_start_and_message() -> None:
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)

    assert await handler.dispatch("ping", {}) == {"pong": True}
    assert await handler.dispatch("start", {}) == {"started": True}
    assert await handler.dispatch("message", {"text": "try trees"}) == {
        "response": "accepted"
    }
    assert runtime.started is True
    assert runtime.messages == ["try trees"]


@pytest.mark.asyncio
async def test_handler_maps_exact_controls_to_messages() -> None:
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)

    for method in ("pause", "resume", "stop"):
        assert await handler.dispatch(method, {}) == {"status": "accepted"}

    assert runtime.messages == ["/pause", "/resume", "/stop"]


@pytest.mark.asyncio
async def test_handler_wires_start_validation_to_runtime() -> None:
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)

    assert await handler.dispatch("start_validation", {}) == {"status": "COMPLETED"}


@pytest.mark.asyncio
async def test_handler_session_switch_swaps_runtime(tmp_path) -> None:
    created: list[tuple[str, Path | None]] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        created.append((root, state_root))
        return RecordingRuntime()

    handler = _handler_at(tmp_path, factory)

    result = await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert result == {"session_id": "s-1", "records": []}
    assert len(created) == 1
    assert created[0] == (str(tmp_path), tmp_path / ".athena" / "conversations" / "s-1")


@pytest.mark.asyncio
async def test_handler_swap_failure_keeps_previous_runtime_open(tmp_path) -> None:
    runtime = RecordingRuntime()

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        del root, state_root
        raise RuntimeError("cannot build runtime")

    handler = GuiRequestHandler(runtime, factory)

    with pytest.raises(RuntimeError, match="cannot build runtime"):
        await handler.dispatch("session_switch", {"session_id": "s-1"})

    # A failed swap must not close the old runtime, or every later RPC fails
    # with "runtime is closed".
    assert handler.runtime is runtime
    assert await handler.dispatch("ping", {}) == {"pong": True}


@pytest.mark.asyncio
async def test_handler_session_switch_default_has_no_state_root(tmp_path) -> None:
    created: list[Path | None] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        created.append(state_root)
        return RecordingRuntime()

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "default"})
    assert created == [None]


@pytest.mark.asyncio
async def test_handler_session_switch_resumes_running_search(tmp_path) -> None:
    """SEARCH/RUNNING 且已持久化的会话在切换时自动续跑（断点续传）。"""
    from types import SimpleNamespace

    created: list[RecordingRuntime] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        state_path = tmp_path / ".athena" / "conversations" / "s-1" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{}", encoding="utf-8")
        runtime._state_path = state_path
        runtime.state = SimpleNamespace(phase="SEARCH", status="RUNNING")
        created.append(runtime)
        return runtime

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert created[0].started is True


@pytest.mark.asyncio
async def test_handler_session_switch_does_not_resume_fresh_session(tmp_path) -> None:
    """全新会话的内存默认状态（SEARCH/RUNNING）不能被误判成需要续跑。"""
    from types import SimpleNamespace

    created: list[RecordingRuntime] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.state = SimpleNamespace(phase="SEARCH", status="RUNNING")
        created.append(runtime)
        return runtime

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert created[0].started is False


@pytest.mark.asyncio
async def test_handler_session_switch_does_not_resume_completed(tmp_path) -> None:
    """COMPLETED/WAITING 会话在切换时不自动续跑。"""
    from types import SimpleNamespace

    created: list[RecordingRuntime] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.state = SimpleNamespace(phase="SEARCH", status="COMPLETED")
        created.append(runtime)
        return runtime

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert created[0].started is False


@pytest.mark.asyncio
async def test_handler_session_switch_suspends_running_runtime_before_close(
    tmp_path,
) -> None:
    """切走一个正在跑的会话：先把 RUNNING 落成 WAITING，再关掉它的 runtime。"""
    outgoing = RecordingRuntime()
    outgoing.project_root = str(tmp_path)
    state_path = tmp_path / ".athena" / "state.json"
    outgoing.state = _running_state()
    outgoing._state_path = state_path
    outgoing.state.save(state_path)

    handler = GuiRequestHandler(outgoing, lambda root, state_root: RecordingRuntime())

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert outgoing.calls == ["suspend", "aclose"]
    assert ResearchState.load(state_path).status == "WAITING"


@pytest.mark.asyncio
async def test_handler_session_switch_downgrades_unresumed_prepare_to_waiting(
    tmp_path,
) -> None:
    """PREPARE 不续跑：状态必须降级为 WAITING，否则前端显示悬空的「运行中」。"""
    created: list[RecordingRuntime] = []
    state_path = tmp_path / ".athena" / "conversations" / "s-1" / "state.json"

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        state = _running_state(phase="PREPARE")
        state.save(state_path)
        runtime.state = state
        runtime._state_path = state_path
        created.append(runtime)
        return runtime

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert created[0].started is False
    assert created[0].state.status == "WAITING"
    assert ResearchState.load(state_path).status == "WAITING"


@pytest.mark.asyncio
async def test_handler_session_switch_does_not_suspend_a_fresh_session(
    tmp_path,
) -> None:
    """全新会话没落过盘：默认的 SEARCH/RUNNING 不是悬空态，不能被降级。"""
    created: list[RecordingRuntime] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.state = _running_state()
        runtime._state_path = (
            tmp_path / ".athena" / "conversations" / "s-1" / "state.json"
        )
        created.append(runtime)
        return runtime

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert "suspend" not in created[0].calls
    assert created[0].state.status == "RUNNING"


@pytest.mark.asyncio
async def test_handler_swap_survives_a_failed_state_persist(tmp_path) -> None:
    """suspend 落盘失败（磁盘满/权限）只该少一条状态记录，不该卡住会话切换。"""
    outgoing = RecordingRuntime()
    outgoing.project_root = str(tmp_path)
    outgoing.suspend_error = OSError("state.json is not writable")
    incoming = RecordingRuntime()

    handler = GuiRequestHandler(outgoing, lambda root, state_root: incoming)

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert outgoing.calls == ["suspend", "aclose"]
    assert handler.runtime is incoming


@pytest.mark.asyncio
async def test_handler_sessions_list_lists_namespaces(tmp_path) -> None:
    handler = _handler_at(tmp_path)
    for sid in ("s-1", "s-2"):
        (tmp_path / ".athena" / "conversations" / sid).mkdir(parents=True)

    result = await handler.dispatch("sessions_list", {})

    assert result["sessions"][0] == "default"
    assert set(result["sessions"]) == {"default", "s-1", "s-2"}


@pytest.mark.asyncio
async def test_handler_session_delete_removes_named_session(tmp_path) -> None:
    handler = _handler_at(tmp_path)
    for sid in ("s-1", "s-2"):
        (tmp_path / ".athena" / "conversations" / sid).mkdir(parents=True)

    result = await handler.dispatch("session_delete", {"session_id": "s-1"})

    assert result["deleted"] is True
    assert set(result["sessions"]) == {"default", "s-2"}
    assert not (tmp_path / ".athena" / "conversations" / "s-1").exists()


@pytest.mark.asyncio
async def test_handler_session_delete_current_swaps_to_default_first(tmp_path) -> None:
    """删除当前会话时先切回 default 释放 runtime 句柄，避免 Windows 删除失败。"""
    created: list[tuple[str, Path | None]] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        created.append((root, state_root))
        runtime = RecordingRuntime()
        runtime.project_root = root
        return runtime

    handler = _handler_at(tmp_path, factory)
    state_root = tmp_path / ".athena" / "conversations" / "s-1"
    state_root.mkdir(parents=True)

    await handler.dispatch("session_switch", {"session_id": "s-1"})
    result = await handler.dispatch("session_delete", {"session_id": "s-1"})

    assert result["deleted"] is True
    assert not state_root.exists()
    assert len(created) == 2
    assert created[1] == (str(tmp_path), None)


@pytest.mark.asyncio
async def test_handler_session_delete_removes_readonly_git_objects(
    tmp_path,
) -> None:
    """删除会话时能清掉 Git 对象文件的只读属性（Windows WinError 5）。"""
    handler = _handler_at(tmp_path)
    state_root = tmp_path / ".athena" / "conversations" / "s-1"
    obj_dir = state_root / "repo" / ".git" / "objects" / "ab"
    obj_dir.mkdir(parents=True)
    obj = obj_dir / "cdef1234"
    obj.write_text("git object", encoding="utf-8")
    os.chmod(obj, stat.S_IREAD)

    result = await handler.dispatch("session_delete", {"session_id": "s-1"})

    assert result["deleted"] is True
    assert not state_root.exists()


@pytest.mark.asyncio
async def test_handler_session_delete_default_clears_transcript(tmp_path) -> None:
    handler = _handler_at(tmp_path)
    log = tmp_path / ".athena" / "logs" / "sessions" / "default.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"type":"user","seq":1,"text":"hi"}\n', encoding="utf-8")

    result = await handler.dispatch("session_delete", {"session_id": "default"})

    assert result["deleted"] is True
    assert not log.exists()


def test_session_state_root_rejects_traversal(tmp_path) -> None:
    assert _session_state_root(tmp_path, "default") is None
    assert (
        _session_state_root(tmp_path, "s-1")
        == tmp_path / ".athena" / "conversations" / "s-1"
    )
    for bad in ("../evil", "a/b", ".", "..", ""):
        with pytest.raises(ValueError, match="invalid session_id"):
            _session_state_root(tmp_path, bad)


@pytest.mark.asyncio
async def test_handler_human_pending_and_reply() -> None:
    """human_pending 返回待回复问题，human_reply 用 request_id 唤醒对应 ask_user。"""
    from gui_gateway.human import HumanRequestBroker

    broker = HumanRequestBroker()
    handler = GuiRequestHandler(RecordingRuntime(), broker=broker)

    async def ask() -> str | None:
        return await broker.ask("accept competition rules?")

    task = asyncio.create_task(ask())
    await asyncio.sleep(0)

    pending = (await handler.dispatch("human_pending", {}))["requests"]
    assert len(pending) == 1
    request_id = pending[0]["request_id"]
    assert pending[0]["prompt"] == "accept competition rules?"

    result = await handler.dispatch(
        "human_reply", {"request_id": request_id, "answer": "done"}
    )
    assert result == {"replied": True}
    assert await task == "done"
    assert (await handler.dispatch("human_pending", {}))["requests"] == []


@pytest.mark.asyncio
async def test_human_broker_choice_and_skip_replies() -> None:
    broker = HumanRequestBroker()
    handler = GuiRequestHandler(RecordingRuntime(), broker=broker)

    async def ask() -> str | None:
        return await broker.ask(
            "choose metric",
            choices=[
                {"label": "accuracy", "value": "accuracy"},
                {"label": "f1", "value": "f1"},
            ],
        )

    task = asyncio.create_task(ask())
    await asyncio.sleep(0)
    request_id = (await handler.dispatch("human_pending", {}))["requests"][0][
        "request_id"
    ]
    assert await handler.dispatch(
        "human_reply", {"request_id": request_id, "choice": "f1"}
    ) == {"replied": True}
    assert await task == "choice:f1"

    async def skip_ask() -> str | None:
        return await broker.ask("skip me")

    task = asyncio.create_task(skip_ask())
    await asyncio.sleep(0)
    request_id = (await handler.dispatch("human_pending", {}))["requests"][0][
        "request_id"
    ]
    assert await handler.dispatch(
        "human_reply", {"request_id": request_id, "skip": True}
    ) == {"replied": True}
    assert await task == "skip"


@pytest.mark.asyncio
async def test_handler_rejects_unknown_methods() -> None:
    handler = GuiRequestHandler(RecordingRuntime())
    with pytest.raises(ValueError, match="unsupported GUI method"):
        await handler.dispatch("STATUS", {})
    with pytest.raises(ValueError, match="message text"):
        await handler.dispatch("message", {})


@pytest.mark.asyncio
async def test_set_project_root_auto_creates_directory(tmp_path) -> None:
    created: list[RecordingRuntime] = []

    def factory(root: str, state_root: Path | None = None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.tree_path = Path(root) / ".athena" / "research_tree.json"
        runtime.project_root = root
        created.append(runtime)
        return runtime

    handler = GuiRequestHandler(RecordingRuntime(), factory)
    target = tmp_path / "nested" / "hahaha"

    result = await handler.dispatch("set_project_root", {"path": str(target)})

    assert target.is_dir()
    assert result["project_root"] == str(target)
    assert len(created) == 1
    assert created[0].tree_path.parent.parent == target
