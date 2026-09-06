import asyncio
import unittest

import pytest

from athena.app_server.protocol import (
    ClientNotification,
    ErrorCode,
    Method,
    RequestEnvelope,
    ResponseEnvelope,
)
from athena.app_server.server import MessageProcessor
from athena.app_server.transport import ServerRequestReply, Transport

from ._support import force_stop_processor


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error: Exception | None = None
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def execute(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return {"method": method, "params": params}


class MessageProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = Transport(control_capacity=64, event_capacity=64)
        self.executor = RecordingExecutor()
        self.processor = MessageProcessor(self.transport, max_inflight=8)
        self.processor.set_executor(self.executor)
        await self.processor.start()

    async def asyncTearDown(self) -> None:
        await force_stop_processor(self.processor)

    async def request(
        self, request_id: int, method: str, params: dict | None = None
    ) -> ResponseEnvelope:
        await self.transport.send_request(
            RequestEnvelope(request_id=request_id, method=method, params=params)
        )
        response = await asyncio.wait_for(
            self.transport.recv_response_or_control(), timeout=0.1
        )
        self.assertIsInstance(response, ResponseEnvelope)
        return response

    async def initialize(self) -> ResponseEnvelope:
        response = await self.request(
            0,
            Method.INITIALIZE,
            {
                "client_name": "test",
                "client_version": "1",
                "protocol_version": 1,
            },
        )
        await self.transport.send_notification(
            ClientNotification(method=Method.INITIALIZED, params={})
        )
        await asyncio.wait_for(self.processor.ready.wait(), timeout=0.1)
        return response

    async def test_start_enters_initializing_state(self) -> None:
        self.assertEqual(self.processor.state, "INITIALIZING")

    async def test_initialize_then_initialized_reaches_ready(self) -> None:
        response = await self.initialize()
        self.assertIsNone(response.error)
        self.assertEqual(response.result["protocol_version"], 1)
        self.assertEqual(self.processor.state, "READY")
        self.assertTrue(self.transport._ready.is_set())

    async def test_initialize_requires_reserved_zero_id(self) -> None:
        response = await self.request(1, Method.INITIALIZE, {"protocol_version": 1})
        self.assertEqual(response.error.code, ErrorCode.INVALID_ARGUMENT.value)

    async def test_unsupported_protocol_version_is_invalid_argument(self) -> None:
        response = await self.request(0, Method.INITIALIZE, {"protocol_version": 999})
        self.assertEqual(response.error.code, ErrorCode.INVALID_ARGUMENT.value)
        self.assertEqual(response.error.message, "request failed")

    async def test_business_request_before_ready_is_not_initialized(self) -> None:
        response = await self.request(1, Method.THREAD_START, {})
        self.assertEqual(response.error.code, ErrorCode.NOT_INITIALIZED.value)
        self.assertEqual(self.executor.calls, [])

    async def test_ready_request_is_forwarded_to_executor(self) -> None:
        await self.initialize()
        response = await self.request(1, Method.THREAD_START, {"session_id": "s"})
        self.assertEqual(
            response.result,
            {"method": Method.THREAD_START, "params": {"session_id": "s"}},
        )

    async def test_duplicate_inflight_request_id_is_rejected(self) -> None:
        await self.initialize()
        self.executor.release = asyncio.Event()
        await self.transport.send_request(
            RequestEnvelope(request_id=1, method=Method.THREAD_START, params={})
        )
        await asyncio.wait_for(self.executor.started.wait(), timeout=0.1)
        await self.transport.send_request(
            RequestEnvelope(request_id=1, method=Method.THREAD_START, params={})
        )
        duplicate = await asyncio.wait_for(
            self.transport.recv_response_or_control(), timeout=0.1
        )
        self.assertEqual(duplicate.error.code, ErrorCode.DUPLICATE_REQUEST_ID.value)
        self.executor.release.set()
        success = await asyncio.wait_for(
            self.transport.recv_response_or_control(), timeout=0.1
        )
        self.assertIsNone(success.error)

    async def test_executor_exception_is_sanitized(self) -> None:
        await self.initialize()
        self.executor.error = OSError(r"C:\\private\\secret-prompt.txt")
        response = await self.request(1, Method.THREAD_START, {})
        rendered = response.model_dump_json()
        self.assertEqual(response.error.code, ErrorCode.INTERNAL.value)
        self.assertEqual(response.error.message, "request failed")
        self.assertNotIn("secret-prompt", rendered)

    async def test_external_shutdown_terminates_dispatcher(self) -> None:
        await self.processor.shutdown(timeout=0.1)
        self.assertEqual(self.processor.state, "TERMINATED")
        self.assertTrue(self.processor._dispatcher_task.done())

    async def test_approval_round_trip_returns_boolean_result(self) -> None:
        approval = asyncio.create_task(
            self.processor.request_approval(
                "thread:1", "turn:1", "approve?", timeout=0.1
            )
        )
        request = await asyncio.wait_for(
            self.transport.recv_response_or_control(), timeout=0.1
        )
        await self.transport.send_server_request_reply(
            ServerRequestReply(request.server_call_id, result={"approved": True})
        )
        self.assertTrue(await asyncio.wait_for(approval, timeout=0.1))

    async def test_shutdown_request_returns_response(self) -> None:
        await self.initialize()
        await self.transport.send_request(
            RequestEnvelope(request_id=1, method=Method.SERVER_SHUTDOWN, params={})
        )
        response = await asyncio.wait_for(
            self.transport.recv_response_or_control(), timeout=0.05
        )
        self.assertEqual(response.result, {"status": "shutting_down"})

    async def test_user_input_round_trip_returns_answers(self) -> None:
        pending = asyncio.create_task(
            self.processor.request_user_input(
                "thread:1", "turn:1", [{"id": "q1", "question": "which?"}], timeout=0.1
            )
        )
        request = await asyncio.wait_for(
            self.transport.recv_response_or_control(), timeout=0.1
        )
        self.assertEqual(request.method, Method.ITEM_USER_INPUT_REQUEST)
        self.assertEqual(
            request.params["questions"], [{"id": "q1", "question": "which?"}]
        )
        await self.transport.send_server_request_reply(
            ServerRequestReply(
                request.server_call_id,
                result={"answers": {"q1": {"answers": ["A"]}}},
            )
        )
        result = await asyncio.wait_for(pending, timeout=0.1)
        self.assertEqual(result["answers"]["q1"]["answers"], ["A"])

    async def test_user_input_timeout_returns_none(self) -> None:
        result = await self.processor.request_user_input(
            "thread:1", "turn:1", [{"id": "q1", "question": "which?"}], timeout=0.05
        )
        self.assertIsNone(result)
