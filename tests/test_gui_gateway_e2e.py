import json

import pandas as pd
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
    dataset = tmp_path / "dataset.csv"
    pd.DataFrame({"age": range(20), "label": [0, 1] * 10}).to_csv(dataset, index=False)
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
            assert parsed["result"]["intent"] == (
                "classify tabular data, target label, optimize f1"
            )
            assert parsed["result"]["needs_configuration"] is True

            await ws.send(
                json.dumps(
                    {
                        "request_id": 2,
                        "method": "TASK_CONFIGURE",
                        "params": {
                            **parsed["result"],
                            "data_path": str(dataset),
                            "target": "label",
                        },
                    }
                )
            )
            configured = await _response(ws, 2)
            assert configured["result"]["configured"] is True

            await ws.send(json.dumps({"request_id": 3, "method": "RUN", "params": {}}))
            started = await _response(ws, 3)
            assert started["result"]["status"] == "running"

            await ws.send(
                json.dumps({"request_id": 4, "method": "STATUS", "params": {}})
            )
            status = await _response(ws, 4)
            assert status["result"]["execution"]["phase"] == "PREPARE"
    finally:
        server.close()
        await server.wait_closed()
        await runtime.aclose()
