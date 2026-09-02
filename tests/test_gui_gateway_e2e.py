import json
from datetime import UTC, datetime

import pytest
import websockets

from athena.core.human_request import HumanOutcome
from athena.research import ResearchRuntime
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
    server, port = await start_server(test_mode=True, runtime=runtime, port=0)
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


class _AnsweringBroker:
    async def ask(self, request):
        return HumanOutcome(
            request_id=request.request_id,
            kind="skip",
            settled_at=datetime.now(UTC),
        )

    async def cancel_scope(self, session_id: str, scope_id: str):
        return []


@pytest.mark.asyncio
async def test_real_runtime_clarification_round_trip_preserves_stale_error(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path,
        session_id="e2e-session",
        broker=_AnsweringBroker(),
        task_confirmation_gate=True,
    )
    server, port = await start_server(test_mode=True, runtime=runtime, port=0)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "request_id": 1,
                        "method": "task_clarification_start",
                        "params": {"task": "predict churn"},
                    }
                )
            )
            started = await _response(ws, 1)
            draft = started["result"]
            assert draft["status"] == "READY_FOR_CONFIRMATION"

            await ws.send(
                json.dumps(
                    {
                        "request_id": 2,
                        "method": "task_clarification_get",
                        "params": {"draft_id": draft["draft_id"]},
                    }
                )
            )
            loaded = await _response(ws, 2)
            assert loaded["result"]["revision"] == draft["revision"]

            await ws.send(
                json.dumps(
                    {
                        "request_id": 3,
                        "method": "task_clarification_revise",
                        "params": {
                            "draft_id": draft["draft_id"],
                            "revision": draft["revision"] - 1,
                            "instruction": "prefer recall",
                        },
                    }
                )
            )
            stale = await _response(ws, 3)
            assert stale["error"]["data"]["code"] == "stale_revision"
    finally:
        server.close()
        await server.wait_closed()
        await runtime.aclose()
