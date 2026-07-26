"""执行适配层 — 协议方法 → ThreadManager/ThreadHandle 调用映射。"""

import logging
from uuid import uuid4

from athena.app_server.protocol import Method
from athena.app_server.submissions import InterruptTurn, StartTurn

logger = logging.getLogger(__name__)


class ExecutionAdapter:
    """协议 DTO → ThreadManager 调用映射。

    - thread/start: ThreadRuntime + listener 就绪后返回
    - turn/start: SubmissionLoop 接纳后立即返回（不等 runner）
    - turn/interrupt: 中断终态已提交到 Journal 后返回
    """

    def __init__(self, manager, event_handlers, subscriptions) -> None:
        self._manager = manager
        self._event_handlers = event_handlers
        self._subscriptions = subscriptions

    async def execute(self, method: str, params: dict) -> dict:
        match method:
            case Method.THREAD_START:
                handle = await self._manager.start(
                    params["session_id"], params["context_ref"]
                )
                await self._event_handlers.attach(handle.thread_id)
                return {"thread_id": handle.thread_id}

            case Method.TURN_START:
                thread_id = params["thread_id"]
                request_ref = params["request_ref"]
                turn_id = str(uuid4())
                handle = await self._manager.get(thread_id)
                turn = await handle.submit(
                    StartTurn(turn_id=turn_id, request_ref=request_ref)
                )
                return {
                    "turn_id": turn.turn_id if hasattr(turn, "turn_id") else turn_id
                }

            case Method.TURN_INTERRUPT:
                handle = await self._manager.get(params["thread_id"])
                await handle.submit(
                    InterruptTurn(
                        turn_id=params["turn_id"],
                        reason=params.get("reason", "user_requested"),
                    )
                )
                return {"turn_id": params["turn_id"], "status": "interrupted"}

            case Method.THREAD_FORK:
                handle = await self._manager.fork(
                    params["thread_id"], params.get("after_turn_id")
                )
                await self._event_handlers.attach(handle.thread_id)
                return {"thread_id": handle.thread_id}

            case Method.THREAD_SUBSCRIBE:
                sub = await self._subscriptions.create(
                    params["thread_id"], params.get("after_sequence", 0)
                )
                return {"subscription_id": sub.subscription_id}

            case Method.THREAD_UNSUBSCRIBE:
                await self._subscriptions.remove(params["subscription_id"])
                return {"status": "unsubscribed"}

            case _:
                raise ValueError(f"unknown method: {method}")
