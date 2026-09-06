import asyncio
import json
from datetime import UTC, datetime

import pytest
import websockets

from athena.core.human_request import HumanOutcome
from athena.research import ResearchRuntime
from athena.research.config import (
    ResearchOptions,
    RuntimeDependencies,
    SessionConfig,
    TaskConfig,
)
from gui_gateway.__main__ import start_server
from tests.integration.research.test_continue_resume_surfaces import (
    assert_confirmed_contract_unchanged,
    build_phase_failure_harness,
)
from tests.unit._support import make_project


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
        session=SessionConfig(session_id="e2e-session"),
        research=ResearchOptions(task=TaskConfig(confirmation_gate=True)),
        dependencies=RuntimeDependencies(broker=_AnsweringBroker()),
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


@pytest.mark.asyncio
async def test_websocket_continue_resumes_failed_confirmed_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    harness = await build_phase_failure_harness(tmp_path, monkeypatch, "PREPARE")
    server, port = await start_server(test_mode=True, runtime=harness.runtime, port=0)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "request_id": 1,
                        "method": "message",
                        "params": {"text": "continue"},
                    }
                )
            )
            resumed = await _response(ws, 1)
            assert resumed["result"] == {"response": "RUNNING"}
            await asyncio.wait_for(harness.second_entered.wait(), timeout=1)

            second_task = harness.runtime.session.lifecycle.task
            assert second_task is not None
            assert second_task is not harness.first_task
            assert harness.runtime.state.status == "RUNNING"
            assert harness.runtime.state.phase == "PREPARE"
            assert_confirmed_contract_unchanged(harness)
    finally:
        server.close()
        await server.wait_closed()
        await harness.close()
