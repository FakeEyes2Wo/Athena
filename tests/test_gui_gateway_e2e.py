import json

import pytest
import websockets

from gui_gateway.__main__ import start_server
from test.unit._support import make_project


async def _response(ws, request_id: int) -> dict:
    while True:
        message = json.loads(await ws.recv())
        if message.get("request_id") == request_id:
            return message


@pytest.mark.asyncio
async def test_full_chat_flow(tmp_path) -> None:
    runtime = make_project(tmp_path)
    server, port = await start_server(test_mode=True, runtime=runtime)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"request_id": 1, "method": "ping", "params": {}}))
            pong = await _response(ws, 1)
            assert pong["result"] == {"pong": True}

            await ws.send(
                json.dumps({"request_id": 2, "method": "start", "params": {}})
            )
            started = await _response(ws, 2)
            assert started["result"] == {"started": True}
    finally:
        server.close()
        await server.wait_closed()
        await runtime.aclose()
