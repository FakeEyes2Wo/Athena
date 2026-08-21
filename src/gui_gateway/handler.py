"""Thin GUI adapter exposing the full research runtime over RPC.

Routes canonical GUI methods (see ``docs/athena-gui-design.md`` §3) to
``GuiService``. The ``runtime`` attribute is retained for the WebSocket
transport, which subscribes to runtime ``state``/``output`` events directly.
"""

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any
from collections.abc import Callable

from athena.gui.service import GuiService
from athena.research import ResearchRuntime
from gui_gateway.human import HumanRequestBroker

logger = logging.getLogger(__name__)

RuntimeFactory = Callable[[str | None, Path | None], ResearchRuntime]

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


async def _rmtree_when_released(path: Path, attempts: int = 5) -> None:
    """删除目录树；Windows 句柄释放有延迟，短暂重试后再放弃。"""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(0.1)


class GuiRequestHandler:
    """Expose runtime control, tree/graph queries, settings, traces, and experiments."""

    def __init__(
        self,
        runtime: ResearchRuntime,
        make_runtime: RuntimeFactory | None = None,
        broker: HumanRequestBroker | None = None,
    ) -> None:
        self._runtime = runtime
        self._make_runtime = make_runtime
        self._broker = broker or HumanRequestBroker()
        self._service = GuiService(runtime, broker=self._broker)
        self._project_root = Path(runtime.settings().get("project_root") or ".")
        self._current_session_id = "default"

    @property
    def runtime(self) -> ResearchRuntime:
        """当前 runtime（transport 订阅用；可被 set_project_root/session_switch 替换）。"""
        return self._runtime

    async def _swap_runtime(self, project_root: str, state_root: Path | None) -> None:
        """Close the current runtime and rebuild a fresh one (project/session swap)."""
        if state_root is not None:
            # 新会话必须立刻落盘，否则 sessions_list 只列已存在目录，刷新后会话消失。
            state_root.mkdir(parents=True, exist_ok=True)
        await self._runtime.aclose()
        self._runtime = self._make_runtime(project_root, state_root)
        self._service = GuiService(self._runtime, broker=self._broker)
        await self._resume_running_session()

    async def _resume_running_session(self) -> None:
        """重建 runtime 后自动续跑「进行中」的会话，实现断点续传。

        只续跑 SEARCH/VALIDATE 且状态为 RUNNING 的会话；COMPLETED/FAILED/STOPPED
        保持静止，WAITING（等待人工决策）也不自动续跑，避免跳过人工决策或重跑
        已完成阶段。续跑失败降级为静默空闲，不阻断会话切换本身。
        """
        try:
            state = getattr(self._runtime, "state", None)
            state_path = getattr(self._runtime, "_state_path", None)
            # 只有从磁盘恢复出的状态才可能是“进行中”；全新会话的内存默认状态
            # （phase=SEARCH/status=RUNNING）不能被误判成需要续跑。
            persisted = state_path is not None and Path(state_path).is_file()
            mid_run = (
                persisted
                and state is not None
                and state.phase in {"SEARCH", "VALIDATE"}
                and state.status == "RUNNING"
            )
        except Exception:
            return
        if mid_run:
            try:
                await self._runtime.start()
            except Exception:
                logger.exception("auto-resume failed; leaving the session idle")

    async def set_project_root(self, path: str) -> dict[str, object]:
        """切换到新项目目录：目录不存在时自动创建，再用工厂重建 runtime。"""
        if self._make_runtime is None:
            raise ValueError("project directory selection is not configured")
        root = Path(path)
        if root.exists() and not root.is_dir():
            raise ValueError(f"project path is not a directory: {root}")
        # 不存在的目录自动创建（含多级父目录），方便首次选择工作区。
        root.mkdir(parents=True, exist_ok=True)
        self._project_root = root
        await self._swap_runtime(str(root), None)
        return self._runtime.settings()

    async def session_switch(self, session_id: str) -> dict[str, object]:
        """切换到独立会话（断点续传）：重建该会话的 runtime 并重放其 transcript。"""
        if self._make_runtime is None:
            raise ValueError("session switching is not configured")
        state_root = _session_state_root(self._project_root, session_id)
        await self._swap_runtime(str(self._project_root), state_root)
        self._current_session_id = session_id
        return {
            "session_id": session_id,
            "records": self._runtime.replay_output_events(),
        }

    def _session_ids(self, project_root: Path | None = None) -> list[str]:
        """返回会话 id（``default`` + 命名空间子目录），最近修改在前。"""
        root = (project_root or self._project_root) / ".athena" / "conversations"
        ids = ["default"]
        if root.is_dir():
            ids.extend(
                p.name
                for p in sorted(
                    root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
                )
                if p.is_dir()
            )
        return ids

    def sessions_list(self) -> dict[str, object]:
        """返回当前工作区的会话 id，最近修改在前。"""
        return {"sessions": self._session_ids()}

    def sessions_list_for(self, path: str) -> dict[str, object]:
        """列出任意工作区目录的会话 id（不切换 runtime），供前端按工作区分组。"""
        return {"sessions": self._session_ids(Path(path))}

    async def session_delete(self, session_id: str) -> dict[str, object]:
        """删除会话；删当前会话时先切回 default 释放句柄，再删目录。"""
        if session_id == "default":
            log = self._project_root / ".athena" / "logs" / "sessions" / "default.jsonl"
            log.unlink(missing_ok=True)
            return {"deleted": True, "sessions": self._session_ids()}
        state_root = _session_state_root(self._project_root, session_id)
        if session_id == self._current_session_id and self._make_runtime is not None:
            await self._swap_runtime(str(self._project_root), None)
            self._current_session_id = "default"
        if state_root is not None and state_root.is_dir():
            await _rmtree_when_released(state_root)
        return {"deleted": True, "sessions": self._session_ids()}

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, object]:
        service = self._service
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
