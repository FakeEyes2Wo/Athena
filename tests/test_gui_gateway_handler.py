import asyncio
import shutil
from pathlib import Path

import pytest

from gui_gateway.handler import GuiRequestHandler, _session_state_root


class RecordingRuntime:
    def __init__(self) -> None:
        self.started = False
        # 模拟 agent rollout writer 持有的文件句柄；aclose() 负责关闭。
        self.open_handle = None
        self.messages: list[str] = []
        self.tree_path = Path("/tmp/athena-runtime")
        self.project_root = "/tmp"

    async def start(self) -> None:
        self.started = True

    async def message(self, text: str) -> str:
        self.messages.append(text)
        return "accepted"

    async def aclose(self) -> None:
        self.started = False
        if self.open_handle is not None:
            self.open_handle.close()
            self.open_handle = None

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
    assert _session_state_root(tmp_path, "s-1") == tmp_path / ".athena" / "conversations" / "s-1"
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


@pytest.mark.asyncio
async def test_handler_session_delete_releases_the_current_session_runtime(
    tmp_path,
) -> None:
    """删除当前所在会话前必须先释放它的 runtime，否则 Windows 上删不掉。

    生产里 agent 的 rollout writer 在 ``{state_root}/logs/agents/*.jsonl`` 上
    持有打开的文件句柄（``runtime.py`` 把 ``rollout_dir`` 指到会话状态根下），
    句柄不放手，``shutil.rmtree`` 会抛 ``PermissionError: [WinError 32]``。
    """

    def factory(root: str, state_root: Path | None) -> RecordingRuntime:
        runtime = RecordingRuntime()
        runtime.project_root = root
        if state_root is not None:
            agents = Path(state_root) / "logs" / "agents"
            agents.mkdir(parents=True, exist_ok=True)
            # 模拟 agent 的 rollout writer：持久句柄，由 aclose() 负责关闭。
            runtime.open_handle = (agents / "agent-0.jsonl").open(
                "a", encoding="utf-8"
            )
        return runtime

    handler = _handler_at(tmp_path, factory)
    await handler.dispatch("session_switch", {"session_id": "s-1"})
    state_root = tmp_path / ".athena" / "conversations" / "s-1"
    assert state_root.is_dir()

    result = await handler.dispatch("session_delete", {"session_id": "s-1"})

    assert result["deleted"] is True
    assert not state_root.exists()
    # 删完不能继续停在一个指向已删目录的 runtime 上。
    assert handler.runtime.open_handle is None


@pytest.mark.asyncio
async def test_handler_session_delete_retries_while_windows_releases_handles(
    tmp_path, monkeypatch
) -> None:
    """句柄释放有延迟：首次 rmtree 抛 PermissionError 时应重试而不是直接失败。"""
    handler = _handler_at(tmp_path)
    (tmp_path / ".athena" / "conversations" / "s-1").mkdir(parents=True)
    calls = {"n": 0}
    original = shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "file in use")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)

    result = await handler.dispatch("session_delete", {"session_id": "s-1"})

    assert calls["n"] == 2
    assert result["deleted"] is True
    assert not (tmp_path / ".athena" / "conversations" / "s-1").exists()
