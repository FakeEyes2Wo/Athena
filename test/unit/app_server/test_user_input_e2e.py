"""端到端：Agent 交互提问 → server 往返 → client 回复 → 模型继续。

验证完整接线：``RequestUserInputTool`` → ``ctx.ask_user``（由 ``agent_runner``
注入）→ ``server.request_user_input`` 发 ``item/userInput/request`` → client
用 ``reply_user_input`` 回复 → 工具拿到回答写回 memory → 采样循环继续 → Turn 完成。
"""

from typing import Any

import pytest

from athena.app_server.lifecycle import AppServer
from athena.app_server.protocol import Method
from athena.app_server.thread_manager import RuntimeThreadManager
from athena.core.agent.provider import ResponsesProvider, StreamEvent
from athena.core.agent.runtime import Agent, agent_runner
from athena.core.agent.tools import RequestUserInputTool
from athena.core.tool import ToolRegistry


class _E2EProvider:
    """第一轮输出 request_user_input 工具调用，第二轮输出最终文本。"""

    def __init__(self):
        self.calls = 0

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                "function_call",
                {
                    "call_id": "c1",
                    "name": "request_user_input",
                    "arguments": {"prompt": "选哪个方案？"},
                },
            )
        else:
            yield StreamEvent(
                "text_delta", {"delta": "采用方案A", "accumulated": "采用方案A"}
            )
        yield StreamEvent("response_completed")


class _Wire:
    """server 由 AppServer.create 创建，runner 构造时尚未就绪 → 晚绑定。"""

    server: Any = None


def _make_ask_user(wire: _Wire):
    def make(thread, turn):
        async def ask(prompt):
            result = await wire.server.request_user_input(
                thread.thread_id,
                turn.turn_id,
                [{"id": "q", "question": prompt}],
                timeout=5.0,
            )
            answers = (result or {}).get("answers", {}).get("q", {}).get("answers", [])
            return answers[0] if answers else None

        return ask

    return make


@pytest.mark.asyncio
async def test_user_input_round_trips_through_app_server() -> None:
    wire = _Wire()
    tools = ToolRegistry()
    tools.register(RequestUserInputTool())
    agent = Agent(ResponsesProvider("model"), tools, "system")
    agent.model = _E2EProvider()

    runner = agent_runner(agent, tools, ask_user=_make_ask_user(wire))
    manager = RuntimeThreadManager(runner)
    app = await AppServer.create(manager, owns_manager=True)
    wire.server = app.server
    client = app.client

    try:
        thread_resp = await client.request(
            "thread/start",
            {"session_id": "s", "context_ref": "artifact://ctx"},
        )
        thread_id = thread_resp["thread_id"]
        turn_resp = await client.request(
            "turn/start", {"thread_id": thread_id, "request_ref": "request"}
        )
        turn_id = turn_resp["turn_id"]

        # 消费事件直到收到提问请求，然后代表用户作答
        answered = False
        while True:
            ev = await client.next_event(timeout=3.0)
            if ev is None:
                break
            if (
                hasattr(ev, "server_call_id")
                and ev.method == Method.ITEM_USER_INPUT_REQUEST
            ):
                await client.reply_user_input(ev.server_call_id, {"q": ["方案A"]})
                answered = True
                break
        assert answered, "未收到 item/userInput/request 请求"

        result_ref = await manager.wait_turn(thread_id, turn_id)
        assert result_ref == f"result://{turn_id}"
        assert agent.model.calls == 2  # 提问轮 + 拿到回答后的终轮
    finally:
        await app.shutdown(timeout=2.0)
