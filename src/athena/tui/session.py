"""协议会话 — TUI 与 app_server 之间唯一的通道。

TUI 的其余部分不认识 ``AthenaClient``、``RuntimeThreadManager``，
只认识这里暴露的几个动词。将来 ``Transport`` 换成 stdio 或 WebSocket，
只需要换 ``TuiSession.create()``。
"""

from uuid import uuid4

from athena.app_server.lifecycle import AppServer
from athena.app_server.protocol import EventNotification, Method, ServerRequest
from athena.app_server.thread_manager import RuntimeThreadManager
from athena.memory.context_manager import ContextManager

DEFAULT_CONTEXT_REF = "context://tui/initial"
SUBMIT_TIMEOUT = 30.0


class TuiSession:
    """一次 TUI 会话：持有 AppServer，管理当前 Thread 与订阅。"""

    def __init__(self, app: AppServer, session_id: str) -> None:
        self._app = app
        self.session_id = session_id
        self.thread_id: str | None = None
        self.subscription_id: str | None = None
        self.threads: list[str] = []

    @classmethod
    async def create(
        cls,
        runner,
        *,
        session_id: str | None = None,
        context_limit: int = 200_000,
    ) -> "TuiSession":
        """装配 ThreadManager + 进程内 AppServer，完成 initialize 握手。"""
        manager = RuntimeThreadManager(
            runner, ctx=ContextManager(context_limit=context_limit)
        )
        app = await AppServer.create(
            manager, owns_manager=True, client_name="athena-tui"
        )
        return cls(app, session_id or f"tui-{uuid4().hex[:8]}")

    @property
    def server(self):
        """底层 MessageProcessor —— 审批闸门需要它发 ServerRequest。"""
        return self._app.server

    async def start_thread(self, context_ref: str = DEFAULT_CONTEXT_REF) -> str:
        """新建 Thread 并订阅其事件流，返回 thread_id。"""
        result = await self._app.client.request(
            Method.THREAD_START,
            {"session_id": self.session_id, "context_ref": context_ref},
        )
        thread_id = result["thread_id"]
        self.threads.append(thread_id)
        await self.switch(thread_id)
        return thread_id

    async def switch(self, thread_id: str, after_sequence: int = 0) -> None:
        """把订阅切到另一个 Thread —— 先退订旧的，避免两路事件混流。"""
        if self.subscription_id is not None:
            await self._app.client.request(
                Method.THREAD_UNSUBSCRIBE, {"subscription_id": self.subscription_id}
            )
            self.subscription_id = None
        result = await self._app.client.request(
            Method.THREAD_SUBSCRIBE,
            {"thread_id": thread_id, "after_sequence": after_sequence},
        )
        self.subscription_id = result["subscription_id"]
        self.thread_id = thread_id

    async def submit(self, text: str) -> str:
        """提交一次用户输入作为新 Turn，返回 turn_id。"""
        if self.thread_id is None:
            raise RuntimeError("no active thread")
        result = await self._app.client.request(
            Method.TURN_START,
            {"thread_id": self.thread_id, "request_ref": text},
            timeout=SUBMIT_TIMEOUT,
        )
        return result["turn_id"]

    async def interrupt(self, turn_id: str, reason: str = "user_interrupt") -> None:
        if self.thread_id is None:
            return
        await self._app.client.request(
            Method.TURN_INTERRUPT,
            {"thread_id": self.thread_id, "turn_id": turn_id, "reason": reason},
        )

    async def fork(self, after_turn_id: str | None = None) -> str:
        """从当前 Thread 分叉出新 Thread，并把订阅切过去。"""
        if self.thread_id is None:
            raise RuntimeError("no active thread")
        params: dict = {"thread_id": self.thread_id}
        if after_turn_id:
            params["after_turn_id"] = after_turn_id
        result = await self._app.client.request(Method.THREAD_FORK, params)
        child = result["thread_id"]
        self.threads.append(child)
        await self.switch(child)
        return child

    async def next_event(
        self, timeout: float | None = None
    ) -> "EventNotification | ServerRequest | None":
        return await self._app.client.next_event(timeout=timeout)

    async def reply_approval(self, server_call_id: str, approved: bool) -> None:
        await self._app.client.respond_to_server_request(
            server_call_id, {"approved": approved}
        )

    async def shutdown(self, timeout: float = 5.0) -> None:
        await self._app.shutdown(timeout=timeout)
