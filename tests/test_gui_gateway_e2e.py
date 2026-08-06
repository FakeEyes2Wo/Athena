import json

import pytest
import websockets

from gui_gateway.__main__ import start_server
from athena.research import ResearchRuntime, ResearchWorkflowDependencies


async def _response(ws, request_id: int) -> dict:
    while True:
        message = json.loads(await ws.recv())
        if message.get("request_id") == request_id:
            return message


class ImmediateSearch:
    async def run(self):
        return []


@pytest.mark.asyncio
async def test_full_chat_flow() -> None:
    async def prepare(task, tree):
        return "exp-baseline"

    runtime = ResearchRuntime(
        dependencies=ResearchWorkflowDependencies(
            prepare_baseline=prepare,
            search_factory=lambda tree, budget, checkpoint: ImmediateSearch(),
        )
    )
    server, port = await start_server(test_mode=True, runtime=runtime)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "request_id": 1,
                        "method": "PARSE_INTENT",
                        "params": {
                            "message": (
                                "classify tabular data, target label, optimize f1"
                            )
                        },
                    }
                )
            )
            parsed = await _response(ws, 1)
            assert parsed["result"]["task_type"] == "classification"
            assert parsed["result"]["primary_metric"] == "f1_macro"

            await ws.send(
                json.dumps(
                    {
                        "request_id": 2,
                        "method": "TASK_CONFIGURE",
                        "params": parsed["result"],
                    }
                )
            )
            configured = await _response(ws, 2)
            assert configured["result"]["configured"] is True

            await ws.send(
                json.dumps(
                    {
                        "request_id": 3,
                        "method": "SEARCH_START",
                        "params": {"max_experiments": 3},
                    }
                )
            )
            started = await _response(ws, 3)
            assert started["result"]["phase"] == "PREPARE"
            assert started["result"]["status"] == "started"
    finally:
        server.close()
        await server.wait_closed()
        await runtime.aclose()
