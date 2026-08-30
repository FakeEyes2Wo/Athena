"""Thin GUI adapter exposing the full research runtime over RPC.

Routes canonical GUI methods (see ``docs/athena-gui-design.md`` §3) to
``GuiService``. The ``runtime`` attribute is retained for the WebSocket
transport, which subscribes to runtime ``state``/``output`` events directly.

会话 runtime 存在一张注册表里：切会话只换「正在看的那个」（transport 据此迁订阅），
正在跑的会话常驻后台不被拆掉。后台会话不订阅也照常落盘，所以切回来只需
``replay_output_events()`` 补对话 + ``subscribe()`` 补状态快照，事件帧无需带会话身份。
代价是看不到后台会话的实时输出，且同一时刻只允许一个会话在跑（见
``_reject_concurrent_run``）。
"""

import asyncio
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Any
from collections.abc import Callable

from athena.gui.service import GuiService
from athena.research import ResearchRuntime
from gui_gateway.human import HumanRequestBroker
from gui_gateway.state_store import GuiState, GuiStateStore

logger = logging.getLogger(__name__)

RuntimeFactory = Callable[[str | None, Path | None], ResearchRuntime]

GUI_REPLAY_LIMIT = 4000
"""``session_switch`` 单次回放给前端的记录条数上限。

transcript 只追加、从不轮转：一个用久了的工作区里 ``default.jsonl`` 会攒到几万条，
全量回放既拖慢切换也把浏览器塞满 DOM 节点。这里只回放最近这些条，更早的记录仍
完整留在磁盘上——断点续传与 task context 走的是 ``replay_output_events()`` 全量
路径，不受这个上限影响。丢掉的条数经 ``truncated`` 如实回传，前端会提示。
"""

# 权威 GUI 方法集合（docs/athena-gui-design.md §3.2 + set_project_root）。
# 与 ``dispatch`` 的分支一一对应；契约测试（tests/test_gui_protocol_contract.py）
# 用它锁定 Python / Rust / TS 三方方法名一致。
SUPPORTED_METHODS: frozenset[str] = frozenset(
    {
        # A. 运行时控制
        "ping",
        "start",
        "start_task",
        "message",
        "pause",
        "resume",
        "stop",
        "parse_intent",
        "start_search",
        "start_validation",
        "generate_report",
        # B. 状态与树
        "state_get",
        "tree_get",
        "tree_save",
        "tree_load",
        "sessions_list",
        "sessions_list_for",
        "session_switch",
        "session_delete",
        "eda_report",
        # C. 假设图与算法
        "hypothesis_graph",
        "graph_algorithms",
        "graph_algorithm",
        # D. 设置
        "settings_get",
        "settings_set",
        "set_project_root",
        # E. LLM I/O 轨迹
        "traces_list",
        "trace_get",
        # F. 实验管理
        "experiments_list",
        "experiment_get",
        "experiment_transition",
        "experiment_set_sota",
        # G. 人类交互（supervisor 的 request_user_input → ask_user）
        "human_pending",
        "human_reply",
    }
)


def _require_str(params: dict[str, Any], key: str, label: str) -> str:
    """从 params 取出非空字符串，否则抛 ValueError（统一各方法的入参校验）。"""
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_dict(params: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    """从 params 取出对象（缺省为 {}），非对象抛 ValueError。"""
    value = params.get(key) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _session_state_root(project_root: Path, session_id: str) -> Path | None:
    """Map a conversation id to its isolated state root (``None`` = default session)."""
    if session_id == "default":
        return None
    if (
        not session_id
        or session_id != Path(session_id).name
        or session_id in {".", ".."}
    ):
        raise ValueError("invalid session_id")
    return project_root / ".athena" / "conversations" / session_id


def _session_activity(state_root: Path) -> tuple[bool, float]:
    """Return ``(has content, last activity)`` for one session state root.

    痕迹 = 持久化状态或 transcript。两者都没有就是空白会话，时间退回目录本身的
    mtime，让刚建出来还没写过东西的新会话仍然排在最前。
    """
    stamps = [
        path.stat().st_mtime
        for path in (
            state_root / "state.json",
            state_root / "logs" / "sessions" / "default.jsonl",
        )
        if path.is_file()
    ]
    if stamps:
        return True, max(stamps)
    return False, state_root.stat().st_mtime if state_root.is_dir() else 0.0


def _is_running(runtime: ResearchRuntime) -> bool:
    """会话是否真的在跑。

    注册表里的 runtime 都是活的，内存里的 ``state.status`` 就是真相——不必像加载侧
    那样先确认这份状态是从磁盘读出来的。
    """
    return getattr(getattr(runtime, "state", None), "status", None) == "RUNNING"


def _rmtree_force(path: Path) -> None:
    """删除目录树；先清除只读属性（Windows Git 对象文件），再删除。"""
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.chmod(os.path.join(root, name), stat.S_IWRITE)
        for name in dirs:
            os.chmod(os.path.join(root, name), stat.S_IWRITE)
    shutil.rmtree(path)


async def _rmtree_when_released(path: Path, attempts: int = 10) -> None:
    """删除目录树；Windows 句柄释放有延迟，按退避重试足够久后再放弃。"""
    delay = 0.1
    for attempt in range(attempts):
        try:
            _rmtree_force(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.5)


class GuiRequestHandler:
    """Expose runtime control, tree/graph queries, settings, traces, and experiments."""

    def __init__(
        self,
        runtime: ResearchRuntime,
        make_runtime: RuntimeFactory | None = None,
        broker: HumanRequestBroker | None = None,
        state_store: GuiStateStore | None = None,
    ) -> None:
        self._runtimes: dict[str, ResearchRuntime] = {"default": runtime}
        self._current_session_id = "default"
        self._make_runtime = make_runtime
        self._broker = broker or HumanRequestBroker()
        self._state_store = state_store or GuiStateStore()
        self._service = GuiService(runtime, broker=self._broker)
        self._service_for: ResearchRuntime = runtime
        self._project_root = Path(runtime.settings().get("project_root") or ".")

    @property
    def runtime(self) -> ResearchRuntime:
        """当前正在看的那个会话的 runtime（transport 据此迁订阅）。"""
        return self._runtimes[self._current_session_id]

    def _current_service(self) -> GuiService:
        """当前会话的 GuiService；runtime 换了就重建，避免两处状态各记一半。"""
        runtime = self.runtime
        if self._service_for is not runtime:
            self._service = GuiService(runtime, broker=self._broker)
            self._service_for = runtime
        return self._service

    async def _activate_session(self, session_id: str) -> None:
        """激活目标会话：注册表里有就直接用，没有才建。

        「取或建」是常驻的全部实现。命中表里活着的 runtime 时既不重建也不
        ``_resume_running_session``——后者是给从磁盘冷启的会话准备的断点续传，对
        内存里的活 runtime 跑一遍要么重进一次生命周期，要么反过来把一个真在跑的
        PREPARE 降级成 WAITING。建新的一律先建后关：构造失败时当前会话原样可用，
        而不是每个 RPC 都撞上已关闭的 AgentRuntime（"runtime is closed"）。
        """
        fresh = session_id not in self._runtimes
        if fresh:
            state_root = _session_state_root(self._project_root, session_id)
            if state_root is not None:
                # 新会话必须立刻落盘，否则 sessions_list 只列已存在目录，刷新后会话消失。
                state_root.mkdir(parents=True, exist_ok=True)
            self._runtimes[session_id] = self._make_runtime(
                str(self._project_root), state_root
            )
        self._current_session_id = session_id
        await self._release_idle_sessions()
        if fresh:
            await self._resume_running_session()

    async def _release_idle_sessions(self) -> None:
        """回收注册表里既不是当前会话、也不在跑的 runtime。

        这是常驻的代价边界：正在跑的会话留活（它照常落盘，切回来靠 replay +
        subscribe 补齐），其余一律 suspend + aclose 并移出表——否则每个点开过的会话
        都会一直占着 git 句柄与显存租约。跑完的后台会话在下一次激活时同样被扫掉。
        """
        for session_id in [
            sid for sid in self._runtimes if sid != self._current_session_id
        ]:
            runtime = self._runtimes[session_id]
            if _is_running(runtime):
                continue
            del self._runtimes[session_id]
            # aclose 马上就要杀掉这个会话的 supervisor，而 Supervisor.stop 从不写
            # status：不先降级就会在磁盘上留下一个"运行中"、实际没人跑的悬空态。
            await self._suspend_runtime(runtime)
            await runtime.aclose()

    def _reject_concurrent_run(self) -> None:
        """并发闸门：同一时刻只允许一个会话在跑，撞上就显式失败，不排队。

        每个 runtime 各自持有 GpuPool、git repo 与 worktree；真并行会让第二个会话在
        显存租约上无限期等待，而用户只看到「点了开始但什么都没发生」。``RuntimeError``
        经 ``map_exception_to_error_code`` 映射为 FAILED_PRECONDITION。
        """
        for session_id, runtime in self._runtimes.items():
            if session_id != self._current_session_id and _is_running(runtime):
                raise RuntimeError(
                    f"session {session_id} is still running; only one session may "
                    "run at a time - pause or stop it first"
                )

    async def _suspend_runtime(self, runtime: ResearchRuntime) -> None:
        """把一个 runtime 的 RUNNING 降级为 WAITING 并落盘（拆卸侧与加载侧共用）。"""
        try:
            await runtime.suspend()
        except OSError:
            # state.json 落盘失败（磁盘满 / 无写权限 / 目录已被删）→ 只记一条日志：
            # 会话切换和快照推送不能因为写不进状态文件就卡住。
            logger.warning("failed to persist the suspended run state", exc_info=True)

    async def _resume_running_session(self) -> None:
        """重建 runtime 后自动续跑「进行中」的会话，实现断点续传。

        只续跑 SEARCH/VALIDATE 且状态为 RUNNING 的会话；COMPLETED/FAILED/STOPPED
        保持静止，WAITING（等待人工决策）也不自动续跑，避免跳过人工决策或重跑
        已完成阶段。PREPARE 一律不续跑：它是一整段协程而不是带检查点的调度循环，
        tree 里还没落下可信 baseline 时重进就会从头重跑 EDA 与基线，点一下侧栏
        会话不该付这个代价——降级为 WAITING，等用户手动点继续。
        续跑失败降级为静默空闲，不阻断会话切换本身。
        """
        runtime = self.runtime
        try:
            state = getattr(runtime, "state", None)
            state_path = getattr(runtime, "_state_path", None)
            # 只有从磁盘恢复出的状态才可能是“进行中”；全新会话的内存默认状态
            # （IDLE/PREPARE）既不该被误判成需要续跑，也不该被降级。
            persisted = state_path is not None and Path(state_path).is_file()
            running = persisted and state is not None and state.status == "RUNNING"
            mid_run = running and state.phase in {"SEARCH", "VALIDATE"}
        except Exception:
            return
        if mid_run:
            try:
                await runtime.start()
            except Exception:
                logger.exception("auto-resume failed; leaving the session idle")
            return
        if running:
            # 不续跑的持久化 RUNNING 就是悬空态（写下它的进程早已退出）：降级并落盘，
            # 让 subscribe 推给前端的第一帧快照等于真实运行状态。
            await self._suspend_runtime(runtime)

    async def set_project_root(self, path: str) -> dict[str, object]:
        """切换到新项目目录：目录不存在时自动创建，再用工厂重建 runtime。"""
        if self._make_runtime is None:
            raise ValueError("project directory selection is not configured")
        root = Path(path)
        if root.exists() and not root.is_dir():
            raise ValueError(f"project path is not a directory: {root}")
        # 不存在的目录自动创建（含多级父目录），方便首次选择工作区。
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        # 先建后关。换工作区就换了一整套会话命名空间（每个工作区都有自己的
        # default），常驻会话——包括正在跑的——一律拆掉，否则旧工作区的会话 id 会
        # 撞上新工作区的同名会话。
        incoming = self._make_runtime(str(root), None)
        outgoing = list(self._runtimes.values())
        self._project_root = root
        self._runtimes = {"default": incoming}
        self._current_session_id = "default"
        for runtime in outgoing:
            await self._suspend_runtime(runtime)
            await runtime.aclose()
        await self._resume_running_session()
        self._state_store.save(
            GuiState(
                active_project_root=str(root),
                last_sessions=self._state_store.load().last_sessions,
            )
        )
        return self.runtime.settings()

    async def session_switch(self, session_id: str) -> dict[str, object]:
        """切到目标会话并重放其 transcript：只换正在看的那个，不打断正在跑的那个。"""
        if self._make_runtime is None:
            raise ValueError("session switching is not configured")
        previous = self._current_session_id
        await self._activate_session(session_id)
        if previous != session_id:
            await self._discard_blank_session(previous)
        self._remember_session(session_id)
        records = self.runtime.replay_output_events()
        dropped = max(0, len(records) - GUI_REPLAY_LIMIT)
        return {
            "session_id": session_id,
            "records": records[dropped:],
            "truncated": dropped,
            "sessions": self._session_ids(),
        }

    async def _discard_blank_session(self, session_id: str) -> None:
        """回收刚离开的空白命名会话（没有 state.json 也没有 transcript）。

        以前由前端猜：它靠 view model 和一个切会话时就被清零的运行标记判断
        「这个会话是空的」，猜错就是误删真实会话。判定挪到后端，只看磁盘痕迹。
        删除失败不阻断切换本身——会话仍在列表里，用户可以手动删。
        """
        if session_id in self._runtimes:
            # 还常驻在表里 = 它正在后台跑，只是还没来得及落盘：不是空白会话。
            return
        state_root = _session_state_root(self._project_root, session_id)
        if state_root is None or not state_root.is_dir():
            return
        has_content, _ = _session_activity(state_root)
        if has_content:
            return
        try:
            await _rmtree_when_released(state_root)
        except OSError:
            # 句柄未释放 / 无写权限 → 保留该会话，只记一条日志。
            logger.warning(
                "failed to discard the blank session %s", session_id, exc_info=True
            )

    def _remember_session(self, session_id: str) -> None:
        """记住「本工作区上次用的会话」，供下次挂载时恢复。"""
        try:
            stored = self._state_store.load()
            sessions = dict(stored.last_sessions or {})
            sessions[str(self._project_root)] = session_id
            self._state_store.save(
                GuiState(
                    active_project_root=stored.active_project_root
                    or str(self._project_root),
                    last_sessions=sessions,
                )
            )
        except OSError:
            # 状态文件写不进去（磁盘满 / 无写权限）→ 下次挂载退回列表首位即可。
            logger.warning("failed to persist the last active session", exc_info=True)

    def _session_ids(self, project_root: Path | None = None) -> list[str]:
        """返回会话 id（``default`` + 命名空间子目录），最近活动在前。

        ``default`` 也要有痕迹才算存在，否则每个从没用过的工作区都会在侧栏里
        凭空占一行「新会话」；它同样参与排序，不再硬编码为第一位。
        """
        athena = (project_root or self._project_root) / ".athena"
        entries: list[tuple[float, str]] = []
        has_content, mtime = _session_activity(athena)
        if has_content:
            entries.append((mtime, "default"))
        conversations = athena / "conversations"
        if conversations.is_dir():
            entries.extend(
                (_session_activity(path)[1], path.name)
                for path in conversations.iterdir()
                if path.is_dir()
            )
        return [name for _, name in sorted(entries, key=lambda e: e[0], reverse=True)]

    def _running_ids(self) -> list[str]:
        """当前工作区里正在跑的会话 id（注册表的内存态，不读磁盘）。

        磁盘上的 ``RUNNING`` 只说明"写下它的那个进程认为自己在跑"，跨进程重启后就是
        悬空态；表里的 runtime 都是本进程活着的对象，状态即真相。
        """
        return [sid for sid, runtime in self._runtimes.items() if _is_running(runtime)]

    def sessions_list(self) -> dict[str, object]:
        """返回当前工作区的会话 id（最近活动在前）、上次使用的会话与正在跑的会话。"""
        ids = self._session_ids()
        remembered = (self._state_store.load().last_sessions or {}).get(
            str(self._project_root)
        )
        active = remembered if remembered in ids else (ids[0] if ids else None)
        return {"sessions": ids, "active": active, "running": self._running_ids()}

    def sessions_list_for(self, path: str) -> dict[str, object]:
        """列出任意工作区目录的会话 id（不切换 runtime），供前端按工作区分组。

        运行态只存在于本进程的注册表里，而这条路径通常问的是别的工作区——那里没有
        活 runtime，``running`` 恒为空，语义是"本网关不知道那边在不在跑"而不是
        "那边没在跑"。被问到当前工作区时照常如实回答。
        """
        root = Path(path)
        running = self._running_ids() if root == self._project_root else []
        return {"sessions": self._session_ids(root), "running": running}

    async def session_delete(self, session_id: str) -> dict[str, object]:
        """删除会话；它只要还常驻在注册表里就先 aclose 释放句柄，再删目录。"""
        if session_id == "default":
            await self._reset_default_session()
            return {"deleted": True, "sessions": self._session_ids()}
        state_root = _session_state_root(self._project_root, session_id)
        if session_id == self._current_session_id and self._make_runtime is not None:
            await self._activate_session("default")
        doomed = self._runtimes.pop(session_id, None)
        if doomed is not None:
            # 后台常驻的会话（含正在跑的那个）同样要先还句柄，否则 Windows 删不掉
            # 目录。状态不必落 WAITING——state.json 下一步就没了。
            await doomed.aclose()
        if state_root is not None and state_root.is_dir():
            await _rmtree_when_released(state_root)
        return {"deleted": True, "sessions": self._session_ids()}

    async def _reset_default_session(self) -> None:
        """重置默认会话：释放句柄 → 清掉它的痕迹 → 重开一个干净 runtime。

        default 没有独立目录（状态就落在工作区的 ``.athena/`` 里），所以只删它
        自己的那几份文件：工作区目录本体、``workspaces/``、artifacts 与命名会话
        都保留。顺序不能反——先建新 runtime 会让它读到马上要被删掉的 state.json，
        ``_resume_running_session`` 甚至会把这份悬空的 RUNNING 续跑起来。
        """
        athena = self._project_root / ".athena"
        reopen = self._make_runtime is not None
        doomed = self._runtimes.pop("default", None) if reopen else None
        if doomed is not None:
            await self._suspend_runtime(doomed)
            await doomed.aclose()
        try:
            sessions_dir = athena / "logs" / "sessions"
            if sessions_dir.is_dir():
                await _rmtree_when_released(sessions_dir)
            for name in ("state.json", "resume.json", "research_tree.json"):
                (athena / name).unlink(missing_ok=True)
        finally:
            # 删不掉（句柄占用）也要留下一个能继续服务的 runtime；异常照常上抛给
            # transport，前端才看得到真实原因。
            if reopen:
                self._runtimes["default"] = self._make_runtime(
                    str(self._project_root), None
                )
        self._current_session_id = "default"

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, object]:
        service = self._current_service()
        if method in {"start", "start_search"}:
            self._reject_concurrent_run()
        if method == "ping":
            return await service.ping()
        if method == "start":
            return await service.start()
        if method == "start_task":
            return await service.start_task(_require_str(params, "task", "task"))
        if method == "message":
            return await service.message(_require_str(params, "text", "message text"))
        if method in {"pause", "resume", "stop"}:
            return await getattr(service, method)()
        if method == "parse_intent":
            return await service.parse_intent(
                _require_str(params, "message", "message")
            )
        if method == "start_search":
            return await service.start_search(_require_dict(params, "config", "config"))
        if method == "start_validation":
            return await service.start_validation()
        if method == "generate_report":
            return await service.generate_report()

        if method == "state_get":
            return service.state_get()
        if method == "tree_get":
            return service.tree_get()
        if method == "tree_save":
            return service.tree_save()
        if method == "tree_load":
            return service.tree_load()
        if method == "sessions_list":
            return self.sessions_list()
        if method == "sessions_list_for":
            return self.sessions_list_for(_require_str(params, "path", "path"))
        if method == "session_switch":
            return await self.session_switch(
                _require_str(params, "session_id", "session_id")
            )
        if method == "session_delete":
            return await self.session_delete(
                _require_str(params, "session_id", "session_id")
            )
        if method == "eda_report":
            return service.eda_report()

        if method == "hypothesis_graph":
            return service.hypothesis_graph()
        if method == "graph_algorithms":
            return service.graph_algorithms()
        if method == "graph_algorithm":
            return service.graph_algorithm(
                _require_str(params, "name", "algorithm name"),
                _require_dict(params, "params", "algorithm params"),
            )

        if method == "settings_get":
            return service.settings_get()
        if method == "settings_set":
            return await service.settings_set(
                _require_dict(params, "patch", "settings patch")
            )
        if method == "set_project_root":
            return await self.set_project_root(
                _require_str(params, "path", "project path")
            )

        if method == "traces_list":
            return service.traces_list()
        if method == "trace_get":
            return service.trace_get(_require_str(params, "agent_id", "agent_id"))

        if method == "experiments_list":
            kind = params.get("kind")
            return service.experiments_list(kind)
        if method == "experiment_get":
            return service.experiment_get(
                _require_str(params, "experiment_id", "experiment_id")
            )
        if method == "experiment_transition":
            return service.experiment_transition(
                _require_str(params, "experiment_id", "experiment_id"),
                _require_str(params, "status", "status"),
                error=params.get("error"),
            )
        if method == "experiment_set_sota":
            return service.experiment_set_sota(
                _require_str(params, "experiment_id", "experiment_id")
            )

        if method == "human_pending":
            return {"requests": self._broker.pending()}
        if method == "human_reply":
            request_id = _require_str(params, "request_id", "request_id")
            answer = params.get("answer")
            if answer is not None and not isinstance(answer, str):
                raise ValueError("answer must be a string")
            choice = params.get("choice")
            if choice is not None and not isinstance(choice, str):
                raise ValueError("choice must be a string")
            skip = bool(params.get("skip", False))
            return {
                "replied": self._broker.reply(
                    request_id,
                    answer,
                    choice=choice,
                    skip=skip,
                )
            }

        raise ValueError(f"unsupported GUI method: {method}")
