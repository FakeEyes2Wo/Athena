import asyncio
import os
import stat
from pathlib import Path

import pytest

from athena.research.supervisor.state import ResearchState
from gui_gateway import state_store
from gui_gateway.handler import GuiRequestHandler, _session_state_root
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
        self.resume_calls = 0
        self.tree_path = Path("/tmp/athena-runtime")
        self.project_root = "/tmp"
        # 拆卸顺序断言用：suspend 必须发生在 aclose 之前。
        self.calls: list[str] = []
        self.suspend_error: Exception | None = None

    async def start(self) -> None:
        self.started = True

    async def message(self, text: str) -> str:
        self.messages.append(text)
        if text == "continue":
            return await self.resume_current_task()
        return "accepted"

    async def resume_current_task(self) -> str:
        self.resume_calls += 1
        return "RUNNING"

    async def suspend(self) -> str:
        """替身版 ``ResearchRuntime.suspend``：真实落盘，状态守卫由真 Supervisor 负责。"""
        self.calls.append("suspend")
        if self.suspend_error is not None:
            raise self.suspend_error
        state = getattr(self, "state", None)
        if state is None or state.status != "RUNNING":
            return getattr(state, "status", "IDLE")
        state.status = "WAITING"
        state.save(self.state_path)
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


class SettingsRuntime(RecordingRuntime):
    def __init__(self, project_root: str, skip_validate: bool = False) -> None:
        super().__init__()
        self.project_root = project_root
        self.skip_validate = skip_validate

    async def apply_settings(self, patch: dict[str, object]) -> dict[str, object]:
        if "skip_validate" in patch:
            self.skip_validate = bool(patch["skip_validate"])
        return {
            "project_root": self.project_root,
            "skip_validate": self.skip_validate,
        }

    def settings(self) -> dict[str, object]:
        return {
            "project_root": self.project_root,
            "skip_validate": self.skip_validate,
        }


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
async def test_handler_routes_resume_to_the_public_runtime_operation() -> None:
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)

    for method in ("pause", "stop"):
        assert await handler.dispatch(method, {}) == {"status": "accepted"}

    assert await handler.dispatch("resume", {}) == {"status": "RUNNING"}

    assert runtime.messages == ["/pause", "/stop"]
    assert runtime.resume_calls == 1


@pytest.mark.asyncio
async def test_handler_keeps_exact_continue_on_the_runtime_message_path() -> None:
    """Free text uses runtime parsing, so it can intercept continue before clarification."""
    runtime = RecordingRuntime()
    handler = GuiRequestHandler(runtime)

    assert await handler.dispatch("message", {"text": "continue"}) == {
        "response": "RUNNING"
    }
    assert runtime.messages == ["continue"]
    assert runtime.resume_calls == 1


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

    assert result == {"session_id": "s-1", "records": [], "sessions": ["s-1"]}
    assert len(created) == 1
    assert created[0] == (str(tmp_path), tmp_path / ".athena" / "conversations" / "s-1")


@pytest.mark.asyncio
async def test_settings_set_persists_validated_skip_preference_and_state(
    tmp_path,
) -> None:
    store = GuiStateStore(tmp_path / "gui_state.json")
    store.save(
        GuiState(
            active_project_root=str(tmp_path),
            last_sessions={str(tmp_path.resolve()): "s-1"},
        )
    )
    runtime = SettingsRuntime(str(tmp_path))
    handler = GuiRequestHandler(runtime, state_store=store)

    result = await handler.dispatch("settings_set", {"patch": {"skip_validate": True}})
    saved = store.load()

    assert result["skip_validate"] is True
    assert saved.skip_validate_for(tmp_path) is True
    assert saved.active_project_root == str(tmp_path.resolve())
    assert saved.last_sessions == {str(tmp_path.resolve()): "s-1"}


@pytest.mark.asyncio
async def test_session_switch_restores_project_skip_preference_and_isolates_projects(
    tmp_path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    store = GuiStateStore(tmp_path / "gui_state.json")
    store.save(
        GuiState(
            active_project_root=str(tmp_path),
            skip_validate_by_project={str(tmp_path.resolve()): True},
        )
    )
    created: list[SettingsRuntime] = []

    def factory(
        root: str, state_root: Path | None, *, skip_validate: bool = False
    ) -> SettingsRuntime:
        del state_root
        runtime = SettingsRuntime(root, skip_validate)
        created.append(runtime)
        return runtime

    handler = GuiRequestHandler(
        SettingsRuntime(str(tmp_path)), factory, state_store=store
    )

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert created[-1].skip_validate is True
    await handler.dispatch("set_project_root", {"path": str(other)})
    assert created[-1].skip_validate is False


@pytest.mark.asyncio
async def test_skip_validate_survives_sessions_and_gateway_restart_per_project(
    tmp_path: Path,
) -> None:
    """The project preference is shared by sessions and recreated gateways."""
    other = tmp_path / "other"
    other.mkdir()
    store = GuiStateStore(tmp_path / "gui-state.json")
    created: list[tuple[str, Path | None, bool]] = []

    def factory(
        root: str, state_root: Path | None, *, skip_validate: bool = False
    ) -> SettingsRuntime:
        created.append((root, state_root, skip_validate))
        return SettingsRuntime(root, skip_validate)

    project = str(tmp_path)
    first = factory(
        project, None, skip_validate=store.load().skip_validate_for(project)
    )
    handler = GuiRequestHandler(first, factory, state_store=store)
    await handler.dispatch("settings_set", {"patch": {"skip_validate": True}})

    await handler.dispatch("session_switch", {"session_id": "s-1"})
    await handler.dispatch("session_switch", {"session_id": "s-2"})
    same_project = [entry for entry in created if entry[0] == project]
    assert [entry[1] for entry in same_project] == [
        None,
        tmp_path / ".athena" / "conversations" / "s-1",
        tmp_path / ".athena" / "conversations" / "s-2",
    ]
    assert [entry[2] for entry in same_project] == [False, True, True]

    # A new gateway/store instance reads the durable project map before building
    # both its default runtime and the next named session.
    restarted_store = GuiStateStore(tmp_path / "gui-state.json")
    restarted = factory(
        project,
        None,
        skip_validate=restarted_store.load().skip_validate_for(project),
    )
    restarted_handler = GuiRequestHandler(
        restarted, factory, state_store=restarted_store
    )
    assert restarted.skip_validate is True
    await restarted_handler.dispatch("session_switch", {"session_id": "s-3"})
    assert created[-1] == (
        project,
        tmp_path / ".athena" / "conversations" / "s-3",
        True,
    )

    await restarted_handler.dispatch("set_project_root", {"path": str(other)})
    assert created[-1][0] == str(other.resolve())
    assert created[-1][2] is False
    assert restarted_store.load().skip_validate_for(other) is False


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
        runtime.state_path = state_path
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
    outgoing.state_path = state_path
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
        runtime.state_path = state_path
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
        runtime.state_path = (
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
        _touch(tmp_path / "workspaces" / "conversations" / sid / "REPORT.md")

    result = await handler.dispatch("session_delete", {"session_id": "s-1"})

    assert result["deleted"] is True
    assert set(result["sessions"]) == {"s-2"}
    assert not (tmp_path / ".athena" / "conversations" / "s-1").exists()
    assert not (tmp_path / "workspaces" / "conversations" / "s-1").exists()
    assert (tmp_path / "workspaces" / "conversations" / "s-2").is_dir()


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
    _touch(tmp_path / "workspaces" / "conversations" / "s-1" / "placeholder")
    result = await handler.dispatch("session_switch", {"session_id": "s-2"})

    assert not (tmp_path / ".athena" / "conversations" / "s-1").exists()
    assert not (tmp_path / "workspaces" / "conversations" / "s-1").exists()
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
        GuiState(
            active_project_root=str(other),
            last_sessions={str(other): "s-9"},
            skip_validate_by_project={str(other.resolve()): False},
        )
    )
    handler = _handler_at(tmp_path, lambda root, state_root: RecordingRuntime())

    await handler.dispatch("session_switch", {"session_id": "s-1"})

    assert (await handler.dispatch("sessions_list", {}))["active"] == "s-1"
    saved = store.load()
    assert saved.last_sessions == {str(tmp_path): "s-1", str(other): "s-9"}
    assert saved.skip_validate_by_project == {str(other.resolve()): False}


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
        GuiState(
            active_project_root=str(tmp_path),
            last_sessions={"/old": "s-9"},
            skip_validate_by_project={str(tmp_path.resolve()): True},
        )
    )
    handler = _handler_at(tmp_path, lambda root, state_root: RecordingRuntime())
    target = tmp_path / "another"

    await handler.dispatch("set_project_root", {"path": str(target)})

    saved = store.load()
    assert saved.active_project_root == str(target.resolve())
    assert saved.last_sessions == {"/old": "s-9"}
    assert saved.skip_validate_by_project == {str(tmp_path.resolve()): True}


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


class _ClarificationRuntime(RecordingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.clarification_calls: list[tuple[str, tuple[object, ...]]] = []
        self.confirm_calls: list[tuple[str, int, bool]] = []

    async def task_clarification_start(self, task: str) -> dict:
        self.clarification_calls.append(("start", (task,)))
        return {"draft_id": "draft-1", "revision": 1, "status": "CLARIFYING"}

    async def task_clarification_get(self, draft_id: str) -> dict:
        self.clarification_calls.append(("get", (draft_id,)))
        return {"draft_id": draft_id, "revision": 1, "status": "READY_FOR_CONFIRMATION"}

    async def task_clarification_retry(self, draft_id: str, revision: int) -> dict:
        self.clarification_calls.append(("retry", (draft_id, revision)))
        return {"draft_id": draft_id, "revision": revision, "status": "CLARIFYING"}

    async def task_clarification_revise(
        self, draft_id: str, revision: int, instruction: str
    ) -> dict:
        self.clarification_calls.append(("revise", (draft_id, revision, instruction)))
        return {"draft_id": draft_id, "revision": revision, "status": "CLARIFYING"}

    async def task_clarification_cancel(self, draft_id: str, revision: int) -> dict:
        self.clarification_calls.append(("cancel", (draft_id, revision)))
        return {"draft_id": draft_id, "revision": revision, "status": "CANCELLED"}

    async def confirm_and_start(
        self, draft_id: str, revision: int, acknowledge_unresolved: bool
    ) -> dict:
        self.confirm_calls.append((draft_id, revision, acknowledge_unresolved))
        return {"draft_id": draft_id, "revision": revision, "status": "RUNNING"}


@pytest.mark.asyncio
async def test_handler_dispatches_task_clarification_methods() -> None:
    runtime = _ClarificationRuntime()
    handler = GuiRequestHandler(runtime)

    assert await handler.dispatch(
        "task_clarification_start", {"task": "predict churn"}
    ) == {"draft_id": "draft-1", "revision": 1, "status": "CLARIFYING"}
    assert await handler.dispatch("task_clarification_get", {"draft_id": "draft-1"})
    assert await handler.dispatch(
        "task_clarification_retry", {"draft_id": "draft-1", "revision": 1}
    )
    assert await handler.dispatch(
        "task_clarification_revise",
        {"draft_id": "draft-1", "revision": 1, "instruction": "add target"},
    )
    assert await handler.dispatch(
        "task_clarification_cancel", {"draft_id": "draft-1", "revision": 1}
    )
    assert runtime.clarification_calls == [
        ("start", ("predict churn",)),
        ("get", ("draft-1",)),
        ("retry", ("draft-1", 1)),
        ("revise", ("draft-1", 1, "add target")),
        ("cancel", ("draft-1", 1)),
    ]


@pytest.mark.asyncio
async def test_gated_start_search_calls_confirm_and_start() -> None:
    runtime = _ClarificationRuntime()
    handler = GuiRequestHandler(runtime)

    result = await handler.dispatch(
        "start_search",
        {
            "draft_id": "draft-1",
            "revision": 4,
            "acknowledge_unresolved": True,
        },
    )

    assert result == {"draft_id": "draft-1", "revision": 4, "status": "RUNNING"}
    assert runtime.confirm_calls == [("draft-1", 4, True)]


@pytest.mark.asyncio
async def test_human_reply_accepts_structured_reply_object() -> None:
    from gui_gateway.human import HumanRequestBroker

    broker = HumanRequestBroker()
    handler = GuiRequestHandler(RecordingRuntime(), broker=broker)

    async def ask():
        return await broker.ask(
            "choose metric",
            choices=[
                {"label": "F1", "value": "f1"},
                {"label": "AUC", "value": "auc"},
            ],
        )

    task = asyncio.create_task(ask())
    await asyncio.sleep(0)
    request_id = (await handler.dispatch("human_pending", {}))["requests"][0][
        "request_id"
    ]

    assert await handler.dispatch(
        "human_reply",
        {
            "request_id": request_id,
            "reply": {"kind": "choice", "value": "f1"},
        },
    ) == {"replied": True}
    assert await task == "choice:f1"


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
