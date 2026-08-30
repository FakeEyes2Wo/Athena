import asyncio
import os
import stat
from pathlib import Path

import pytest

from athena.research.supervisor.state import ResearchState
from gui_gateway import state_store
from gui_gateway.handler import (
    GUI_REPLAY_LIMIT,
    GuiRequestHandler,
    _session_state_root,
)
from gui_gateway.human import HumanRequestBroker
from gui_gateway.state_store import GuiState, GuiStateStore


@pytest.fixture(autouse=True)
def isolated_gui_state(tmp_path, monkeypatch):
    """把 GuiStateStore 的默认路径挪进 tmp_path。

    默认是 ``~/.athena/gui_state.json``：没有这层隔离，任何构造 handler 而不注入
    store 的用例都会覆盖开发机上真实的 GUI 配置（活动工作区、每个工作区上次会话）。
    """
    monkeypatch.setattr(state_store, "DEFAULT_STATE_PATH", tmp_path / "gui_state.json")


class RecordingRuntime:
    def __init__(self) -> None:
        self.started = False
        self.messages: list[str] = []
        self.tree_path = Path("/tmp/athena-runtime")
        self.project_root = "/tmp"
        # 拆卸顺序断言用：suspend 必须发生在 aclose 之前。
        self.calls: list[str] = []
        self.suspend_error: Exception | None = None
        # 后台会话在没人订阅的时候仍然落盘：切回来要能把这些记录取回。
        self.records: list[dict] = []

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
        return list(self.records)


def _handler_at(tmp_path: Path, factory=None) -> GuiRequestHandler:
    runtime = RecordingRuntime()
    runtime.project_root = str(tmp_path)
    return GuiRequestHandler(runtime, factory)


def _touch(path: Path, when: float | None = None) -> Path:
    """建一个占位文件（必要时连带父目录），可指定 mtime 以便断言排序。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if when is not None:
        os.utime(path, (when, when))
    return path


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

    assert result == {
        "session_id": "s-1",
        "records": [],
        "truncated": 0,
        "sessions": ["s-1"],
    }
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

    # default 一开始就在注册表里，「取或建」直接命中；先切走再切回来才会重建它。
    await handler.dispatch("session_switch", {"session_id": "s-1"})
    await handler.dispatch("session_switch", {"session_id": "default"})

    assert created == [tmp_path / ".athena" / "conversations" / "s-1", None]


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
async def test_handler_session_switch_keeps_a_running_session_resident(
    tmp_path,
) -> None:
    """切走一个正在跑的会话：留活在注册表里，不 suspend 也不 aclose。"""
    outgoing = RecordingRuntime()
    outgoing.project_root = str(tmp_path)
    state_path = tmp_path / ".athena" / "state.json"
    outgoing.state = _running_state()
    outgoing._state_path = state_path
    outgoing.state.save(state_path)

    handler = GuiRequestHandler(outgoing, lambda root, state_root: RecordingRuntime())

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert outgoing.calls == []
    assert handler._runtimes["default"] is outgoing
    assert outgoing.state.status == "RUNNING"
    assert ResearchState.load(state_path).status == "RUNNING"


@pytest.mark.asyncio
async def test_handler_session_switch_closes_an_idle_session(tmp_path) -> None:
    """切走一个没在跑的会话：仍然先 suspend 落盘再 aclose，并移出注册表。"""
    outgoing = RecordingRuntime()
    outgoing.project_root = str(tmp_path)

    handler = GuiRequestHandler(outgoing, lambda root, state_root: RecordingRuntime())

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert outgoing.calls == ["suspend", "aclose"]
    assert "default" not in handler._runtimes


@pytest.mark.asyncio
async def test_handler_switching_back_reuses_the_resident_runtime(tmp_path) -> None:
    """切回后台会话：命中注册表（不重建、不续跑），并拿到期间追加的 transcript。"""
    created: list[tuple[str, RecordingRuntime]] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.project_root = root
        runtime.state = _running_state()
        created.append((state_root.name if state_root else "default", runtime))
        return runtime

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "s-1"})
    resident = created[-1][1]
    # 没人订阅的这段时间里，后台会话照常往 transcript 里追加记录。
    resident.records.append({"type": "output", "seq": 1, "text": "still training"})

    await handler.dispatch("session_switch", {"session_id": "s-2"})
    result = await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert [sid for sid, _ in created] == ["s-1", "s-2"]
    assert handler.runtime is resident
    assert resident.calls == []
    assert resident.started is False
    assert result["records"] == [{"type": "output", "seq": 1, "text": "still training"}]


@pytest.mark.asyncio
async def test_handler_session_switch_to_the_current_session_is_a_no_op(
    tmp_path,
) -> None:
    """切到当前会话不重建 runtime，但仍回传 sessions 并记住 last-active。"""
    store = GuiStateStore(tmp_path / "gui_state.json")
    handler = _handler_at(tmp_path, lambda root, state_root: RecordingRuntime())
    handler._state_store = store
    _touch(tmp_path / ".athena" / "conversations" / "s-1" / "state.json")

    await handler.dispatch("session_switch", {"session_id": "s-1"})
    resident = handler.runtime
    result = await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert handler.runtime is resident
    assert resident.calls == []
    assert result["sessions"] == ["s-1"]
    assert store.load().last_sessions == {str(tmp_path): "s-1"}


@pytest.mark.asyncio
async def test_handler_session_switch_keeps_a_blank_running_session(tmp_path) -> None:
    """常驻会话还没落盘就切走：不能当成空白会话把它的目录回收掉。"""

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.project_root = root
        runtime.state = _running_state(phase="PREPARE")
        return runtime

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "s-1"})
    result = await handler.dispatch("session_switch", {"session_id": "s-2"})

    assert (tmp_path / ".athena" / "conversations" / "s-1").is_dir()
    assert "s-1" in handler._runtimes
    assert set(result["sessions"]) == {"s-1", "s-2"}


@pytest.mark.asyncio
async def test_handler_releases_a_background_session_once_it_finishes(
    tmp_path,
) -> None:
    """后台会话跑完就不再常驻：下一次激活会把它 suspend + aclose 并移出表。"""
    created: list[RecordingRuntime] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.project_root = root
        runtime.state = _running_state()
        created.append(runtime)
        return runtime

    handler = _handler_at(tmp_path, factory)

    await handler.dispatch("session_switch", {"session_id": "s-1"})
    background = created[0]
    await handler.dispatch("session_switch", {"session_id": "s-2"})
    background.state.status = "COMPLETED"
    await handler.dispatch("session_switch", {"session_id": "s-3"})

    assert background.calls == ["suspend", "aclose"]
    assert "s-1" not in handler._runtimes


@pytest.mark.asyncio
async def test_handler_rejects_a_second_run_while_another_session_runs(
    tmp_path,
) -> None:
    """已有会话在跑时，在另一个会话里 start/start_search 显式失败并点名是谁在跑。"""
    running = RecordingRuntime()
    running.project_root = str(tmp_path)
    running.state = _running_state()

    handler = GuiRequestHandler(running, lambda root, state_root: RecordingRuntime())
    await handler.dispatch("session_switch", {"session_id": "s-1"})

    for method, params in (("start", {}), ("start_search", {"config": {}})):
        with pytest.raises(RuntimeError, match="default"):
            await handler.dispatch(method, params)

    # 被拒绝的一方不能反过来打断正在跑的那个会话。
    assert running.calls == []
    assert running.state.status == "RUNNING"
    assert handler._runtimes["default"] is running


@pytest.mark.asyncio
async def test_handler_allows_starting_the_session_that_is_already_running(
    tmp_path,
) -> None:
    """闸门只拦别的会话：正在跑的那个会话自己仍然可以 start（续跑/重入）。"""
    running = RecordingRuntime()
    running.project_root = str(tmp_path)
    running.state = _running_state()
    handler = GuiRequestHandler(running)

    assert await handler.dispatch("start", {}) == {"started": True}


@pytest.mark.asyncio
async def test_handler_session_delete_closes_a_background_session_first(
    tmp_path,
) -> None:
    """删除后台常驻会话（哪怕正在跑）也先 aclose 释放句柄，再删目录。"""
    created: list[RecordingRuntime] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.project_root = root
        runtime.state = _running_state()
        created.append(runtime)
        return runtime

    handler = _handler_at(tmp_path, factory)
    await handler.dispatch("session_switch", {"session_id": "s-1"})
    background = created[0]
    _touch(tmp_path / ".athena" / "conversations" / "s-1" / "state.json")
    await handler.dispatch("session_switch", {"session_id": "s-2"})

    result = await handler.dispatch("session_delete", {"session_id": "s-1"})

    assert background.calls[-1] == "aclose"
    assert "s-1" not in handler._runtimes
    assert not (tmp_path / ".athena" / "conversations" / "s-1").exists()
    assert result["sessions"] == ["s-2"]


@pytest.mark.asyncio
async def test_handler_failed_activation_leaves_the_registry_untouched(
    tmp_path,
) -> None:
    """目标会话 runtime 构造失败：当前会话与后台常驻会话都不受影响。"""
    running = RecordingRuntime()
    running.project_root = str(tmp_path)
    running.state = _running_state()
    calls: list[Path | None] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        calls.append(state_root)
        if len(calls) > 1:
            raise RuntimeError("cannot build runtime")
        runtime = RecordingRuntime()
        runtime.project_root = root
        return runtime

    handler = GuiRequestHandler(running, factory)
    await handler.dispatch("session_switch", {"session_id": "s-1"})
    survivor = handler.runtime

    with pytest.raises(RuntimeError, match="cannot build runtime"):
        await handler.dispatch("session_switch", {"session_id": "s-2"})

    assert handler.runtime is survivor
    assert "s-2" not in handler._runtimes
    assert handler._runtimes["default"] is running
    assert running.state.status == "RUNNING"
    assert await handler.dispatch("ping", {}) == {"pong": True}


@pytest.mark.asyncio
async def test_set_project_root_suspends_and_closes_every_resident_runtime(
    tmp_path,
) -> None:
    """换工作区换掉一整套会话命名空间：常驻会话（含在跑的）全部 suspend + aclose。"""
    running = RecordingRuntime()
    running.project_root = str(tmp_path)
    state_path = tmp_path / ".athena" / "state.json"
    running.state = _running_state()
    running._state_path = state_path
    running.state.save(state_path)

    handler = GuiRequestHandler(running, lambda root, state_root: RecordingRuntime())
    await handler.dispatch("session_switch", {"session_id": "s-1"})
    viewed = handler.runtime

    await handler.dispatch("set_project_root", {"path": str(tmp_path / "other")})

    assert running.calls == ["suspend", "aclose"]
    assert viewed.calls == ["suspend", "aclose"]
    assert ResearchState.load(state_path).status == "WAITING"
    assert list(handler._runtimes) == ["default"]
    assert handler.runtime is not running
    assert handler.runtime is not viewed


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
    _touch(tmp_path / ".athena" / "logs" / "sessions" / "default.jsonl")
    for sid in ("s-1", "s-2"):
        (tmp_path / ".athena" / "conversations" / sid).mkdir(parents=True)

    result = await handler.dispatch("sessions_list", {})

    assert set(result["sessions"]) == {"default", "s-1", "s-2"}


@pytest.mark.asyncio
async def test_handler_session_delete_removes_named_session(tmp_path) -> None:
    handler = _handler_at(tmp_path)
    for sid in ("s-1", "s-2"):
        (tmp_path / ".athena" / "conversations" / sid).mkdir(parents=True)

    result = await handler.dispatch("session_delete", {"session_id": "s-1"})

    assert result["deleted"] is True
    assert set(result["sessions"]) == {"s-2"}
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


@pytest.mark.asyncio
async def test_handler_sessions_list_is_empty_for_an_untouched_workspace(
    tmp_path,
) -> None:
    """空工作区没有任何会话：default 也要有痕迹才算存在。"""
    handler = _handler_at(tmp_path)

    result = await handler.dispatch("sessions_list", {})

    assert result["sessions"] == []
    assert result["active"] is None


@pytest.mark.asyncio
async def test_handler_sessions_list_shows_default_once_it_has_a_transcript(
    tmp_path,
) -> None:
    handler = _handler_at(tmp_path)
    _touch(tmp_path / ".athena" / "logs" / "sessions" / "default.jsonl")

    result = await handler.dispatch("sessions_list", {})

    assert result["sessions"] == ["default"]


@pytest.mark.asyncio
async def test_handler_sessions_list_shows_default_once_it_has_state(tmp_path) -> None:
    """只落了 state.json（跑过但还没产生 transcript）同样算存在。"""
    handler = _handler_at(tmp_path)
    _touch(tmp_path / ".athena" / "state.json")

    result = await handler.dispatch("sessions_list", {})

    assert result["sessions"] == ["default"]


def test_session_ids_sort_by_mtime_without_pinning_default(tmp_path) -> None:
    """default 参与 mtime 排序，不再恒占首位。"""
    handler = _handler_at(tmp_path)
    transcript = _touch(
        tmp_path / ".athena" / "logs" / "sessions" / "default.jsonl", when=1_000.0
    )
    named = tmp_path / ".athena" / "conversations" / "s-1"
    named.mkdir(parents=True)
    os.utime(named, (2_000.0, 2_000.0))

    assert handler._session_ids() == ["s-1", "default"]

    os.utime(transcript, (3_000.0, 3_000.0))
    assert handler._session_ids() == ["default", "s-1"]


@pytest.mark.asyncio
async def test_handler_session_delete_default_resets_persisted_state(tmp_path) -> None:
    """删 default = 重置默认会话：清痕迹，但保留工作区本体、workspaces/ 与命名会话。"""
    created: list[RecordingRuntime] = []

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.project_root = root
        created.append(runtime)
        return runtime

    handler = _handler_at(tmp_path, factory)
    athena = tmp_path / ".athena"
    for name in ("state.json", "resume.json", "research_tree.json"):
        _touch(athena / name)
    _touch(athena / "logs" / "sessions" / "default.jsonl")
    kept = [
        _touch(tmp_path / "workspaces" / "exp-1" / "train.py"),
        _touch(athena / "conversations" / "s-1" / "state.json"),
        _touch(athena / "artifacts" / "blob.json"),
    ]

    result = await handler.dispatch("session_delete", {"session_id": "default"})

    assert result["deleted"] is True
    assert result["sessions"] == ["s-1"]
    for name in ("state.json", "resume.json", "research_tree.json"):
        assert not (athena / name).exists()
    assert not (athena / "logs" / "sessions" / "default.jsonl").exists()
    assert athena.is_dir()
    assert all(path.exists() for path in kept)
    # 句柄先释放再删除，最后重开一个干净 runtime 供后续 RPC 使用。
    assert created and handler.runtime is created[-1]


@pytest.mark.asyncio
async def test_handler_session_delete_default_reports_a_blocked_deletion(
    tmp_path, monkeypatch
) -> None:
    """删除被占用时如实抛出原因（transport 会把它回传前端），并留下可用 runtime。"""

    async def blocked(path: Path, attempts: int = 10) -> None:
        raise PermissionError("[WinError 32] the file is in use by another process")

    monkeypatch.setattr("gui_gateway.handler._rmtree_when_released", blocked)
    outgoing = RecordingRuntime()
    outgoing.project_root = str(tmp_path)
    handler = GuiRequestHandler(outgoing, lambda root, state_root: RecordingRuntime())
    _touch(tmp_path / ".athena" / "logs" / "sessions" / "default.jsonl")

    with pytest.raises(PermissionError, match="in use by another process"):
        await handler.dispatch("session_delete", {"session_id": "default"})

    # 句柄已经释放掉了：失败也必须留下一个能继续服务的 runtime，而不是关掉的那个。
    assert handler.runtime is not outgoing
    assert outgoing.calls[-1] == "aclose"


@pytest.mark.asyncio
async def test_handler_session_switch_discards_the_blank_session_it_leaves(
    tmp_path,
) -> None:
    """离开一个没留下痕迹的命名会话即回收它——判定在后端，前端不再猜。"""
    handler = _handler_at(tmp_path, lambda root, state_root: RecordingRuntime())

    await handler.dispatch("session_switch", {"session_id": "s-1"})
    result = await handler.dispatch("session_switch", {"session_id": "s-2"})

    assert not (tmp_path / ".athena" / "conversations" / "s-1").exists()
    assert result["sessions"] == ["s-2"]


@pytest.mark.asyncio
async def test_handler_session_switch_keeps_a_session_with_a_transcript(
    tmp_path,
) -> None:
    handler = _handler_at(tmp_path, lambda root, state_root: RecordingRuntime())
    conversations = tmp_path / ".athena" / "conversations"

    await handler.dispatch("session_switch", {"session_id": "s-1"})
    _touch(conversations / "s-1" / "logs" / "sessions" / "default.jsonl")
    result = await handler.dispatch("session_switch", {"session_id": "s-2"})

    assert (conversations / "s-1").is_dir()
    assert set(result["sessions"]) == {"s-1", "s-2"}


@pytest.mark.asyncio
async def test_handler_remembers_the_last_session_per_workspace(tmp_path) -> None:
    """last-active 按工作区隔离：切工作区不会串到另一个工作区的会话。"""
    other = tmp_path / "other"
    other.mkdir()
    store = GuiStateStore(tmp_path / "gui_state.json")
    store.save(
        GuiState(active_project_root=str(other), last_sessions={str(other): "s-9"})
    )
    handler = _handler_at(tmp_path, lambda root, state_root: RecordingRuntime())

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert (await handler.dispatch("sessions_list", {}))["active"] == "s-1"
    assert store.load().last_sessions == {str(tmp_path): "s-1", str(other): "s-9"}


@pytest.mark.asyncio
async def test_handler_sessions_list_active_falls_back_when_the_session_is_gone(
    tmp_path,
) -> None:
    """记住的会话已被删除时退回列表首位，而不是指向一个不存在的会话。"""
    handler = _handler_at(tmp_path, lambda root, state_root: RecordingRuntime())
    GuiStateStore(tmp_path / "gui_state.json").save(
        GuiState(
            active_project_root=str(tmp_path), last_sessions={str(tmp_path): "s-9"}
        )
    )
    _touch(tmp_path / ".athena" / "logs" / "sessions" / "default.jsonl")

    result = await handler.dispatch("sessions_list", {})

    assert result["sessions"] == ["default"]
    assert result["active"] == "default"


@pytest.mark.asyncio
async def test_set_project_root_keeps_remembered_sessions(tmp_path) -> None:
    """切工作区只改活动工作区，不能顺手清空每个工作区的 last-active 记录。"""
    store = GuiStateStore(tmp_path / "gui_state.json")
    store.save(
        GuiState(active_project_root=str(tmp_path), last_sessions={"/old": "s-9"})
    )
    handler = _handler_at(tmp_path, lambda root, state_root: RecordingRuntime())
    target = tmp_path / "another"

    await handler.dispatch("set_project_root", {"path": str(target)})

    saved = store.load()
    assert saved.active_project_root == str(target.resolve())
    assert saved.last_sessions == {"/old": "s-9"}


@pytest.mark.asyncio
async def test_handler_sessions_list_reports_the_running_session(tmp_path) -> None:
    """``running`` 来自注册表的内存态：侧栏据此把后台会话标成运行中。"""

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.project_root = root
        if state_root is not None and state_root.name == "s-1":
            runtime.state = _running_state()
        return runtime

    handler = _handler_at(tmp_path, factory)
    await handler.dispatch("session_switch", {"session_id": "s-1"})
    await handler.dispatch("session_switch", {"session_id": "s-2"})

    result = await handler.dispatch("sessions_list", {})

    assert result["running"] == ["s-1"]
    assert set(result["sessions"]) == {"s-1", "s-2"}


@pytest.mark.asyncio
async def test_handler_sessions_list_reports_nothing_running_when_idle(
    tmp_path,
) -> None:
    handler = _handler_at(tmp_path)
    _touch(tmp_path / ".athena" / "logs" / "sessions" / "default.jsonl")

    result = await handler.dispatch("sessions_list", {})

    assert result["sessions"] == ["default"]
    assert result["running"] == []


@pytest.mark.asyncio
async def test_handler_sessions_list_for_reports_no_run_in_other_workspaces(
    tmp_path,
) -> None:
    """别的工作区没有活 runtime：``running`` 恒为空（「不知道」，不是「没在跑」）。"""
    running = RecordingRuntime()
    running.project_root = str(tmp_path)
    running.state = _running_state()
    handler = GuiRequestHandler(running, lambda root, state_root: RecordingRuntime())
    other = tmp_path / "other"
    _touch(other / ".athena" / "logs" / "sessions" / "default.jsonl")

    assert (await handler.dispatch("sessions_list", {}))["running"] == ["default"]

    elsewhere = await handler.dispatch("sessions_list_for", {"path": str(other)})
    assert elsewhere["sessions"] == ["default"]
    assert elsewhere["running"] == []

    # 同一个工作区被问到时仍然如实回答。
    here = await handler.dispatch("sessions_list_for", {"path": str(tmp_path)})
    assert here["running"] == ["default"]


@pytest.mark.asyncio
async def test_handler_session_switch_caps_the_replayed_transcript(tmp_path) -> None:
    """只回放最近一段：transcript 只追加从不轮转，全量回放会拖死切换。"""
    runtime = RecordingRuntime()
    runtime.project_root = str(tmp_path)
    runtime.records = [
        {"type": "output", "seq": i} for i in range(GUI_REPLAY_LIMIT + 25)
    ]
    handler = GuiRequestHandler(runtime, lambda root, state_root: RecordingRuntime())

    result = await handler.dispatch("session_switch", {"session_id": "default"})

    assert len(result["records"]) == GUI_REPLAY_LIMIT
    # 丢的是最早的那 25 条，留下的是最近的。
    assert result["records"][0]["seq"] == 25
    assert result["records"][-1]["seq"] == GUI_REPLAY_LIMIT + 24
    assert result["truncated"] == 25


@pytest.mark.asyncio
async def test_handler_session_switch_reports_a_complete_transcript(tmp_path) -> None:
    """没超上限就一条不少，``truncated`` 为 0（前端据此决定要不要提示）。"""
    runtime = RecordingRuntime()
    runtime.project_root = str(tmp_path)
    runtime.records = [{"type": "output", "seq": 1}, {"type": "output", "seq": 2}]
    handler = GuiRequestHandler(runtime, lambda root, state_root: RecordingRuntime())

    result = await handler.dispatch("session_switch", {"session_id": "default"})

    assert result["records"] == runtime.records
    assert result["truncated"] == 0


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
