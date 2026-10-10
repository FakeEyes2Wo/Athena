import asyncio
import unittest
from unittest.mock import patch

import pytest

from athena.app_server.client import AthenaClient, ClientWorker, Sequencer
from athena.app_server.exceptions import RpcException
from athena.app_server.protocol import (
    ErrorCode,
    EventNotification,
    ResponseEnvelope,
    RpcError,
    ServerRequest,
)
from athena.app_server.transport import ServerRequestReply, Transport

from ._support import eventually


def reverse_request(index: int) -> ServerRequest:
    return ServerRequest(
        server_call_id=f"server:{index}",
        method="item/approval/request",
        params={"thread_id": "thread:1", "turn_id": "turn:1"},
    )


def notification(sequence: int = 1) -> EventNotification:
    return EventNotification(
        subscription_id="sub:1",
        thread_id="thread:1",
        turn_id="turn:1",
        sequence=sequence,
        kind="item",
        event_ref=f"artifact:{sequence}",
    )


class SequencerTests(unittest.TestCase):
    def test_ids_start_at_one_and_increase(self) -> None:
        sequencer = Sequencer()
        self.assertEqual([sequencer.next() for _ in range(3)], [1, 2, 3])

    def test_overflow_is_rejected(self) -> None:
        sequencer = Sequencer()
        sequencer._next = 2**63
        with self.assertRaises(OverflowError):
            sequencer.next()


class ClientWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = Transport(control_capacity=64, event_capacity=64)
        self.worker = ClientWorker(self.transport)
        await self.worker.start()

    async def asyncTearDown(self) -> None:
        await self.worker.stop()

    async def test_response_resolves_matching_future(self) -> None:
        future = asyncio.get_running_loop().create_future()
        self.worker.register_pending(7, future)
        await self.transport.send_response(
            ResponseEnvelope(request_id=7, result={"ok": True})
        )
        response = await asyncio.wait_for(future, timeout=0.1)
        self.assertEqual(response.result, {"ok": True})
        self.assertIsNone(self.worker.unregister_pending(7))

    async def test_unmatched_response_is_ignored(self) -> None:
        await self.transport.send_response(ResponseEnvelope(request_id=99, result={}))
        await eventually(lambda: self.transport._s2c_control.empty())
        self.assertEqual(self.worker._pending, {})

    async def test_event_is_delivered(self) -> None:
        event = notification()
        await self.transport.send_event(event)
        self.assertIs(await self.worker.next_event(timeout=0.1), event)

    async def test_cancel_after_getter_dequeues_preserves_message(self) -> None:
        event = notification()
        await self.transport.send_event(event)
        original_wait = asyncio.wait

        async def wait_then_cancel(awaitables, **kwargs):
            result = await original_wait(awaitables, **kwargs)
            asyncio.current_task().cancel()
            return result

        with patch("asyncio.wait", side_effect=wait_then_cancel):
            with self.assertRaises(asyncio.CancelledError):
                await self.worker.next_event(timeout=0.1)
        self.assertIs(await self.worker.next_event(timeout=0.1), event)

    async def test_ready_control_is_delivered_before_ready_event(self) -> None:
        await self.transport.send_event(notification())
        await self.transport.send_server_request(reverse_request(1))
        await eventually(
            lambda: not self.worker._event_buf.empty()
            and not self.worker._control_buf.empty()
        )
        self.assertIsInstance(await self.worker.next_event(timeout=0.1), ServerRequest)

    async def test_full_control_buffer_blocks_instead_of_dropping(self) -> None:
        # 控制缓冲区满时阻塞等待而非丢弃 — 审批等关键消息不丢失
        for index in range(17):
            await self.transport.send_server_request(reverse_request(index))
        await eventually(lambda: self.worker._control_buf.qsize() == 16)
        # 消费一条后第 17 条应被放入
        await self.worker.next_event(timeout=0.1)
        await eventually(lambda: self.worker._control_buf.qsize() == 16)

    async def test_fail_all_pending_uses_closed_error_response(self) -> None:
        futures = []
        for request_id in (7, 8):
            future = asyncio.get_running_loop().create_future()
            self.worker.register_pending(request_id, future)
            futures.append((request_id, future))
        self.worker.fail_all_pending(
            RpcError(code=ErrorCode.CLOSED.value, message="client closed")
        )
        for request_id, future in futures:
            response = future.result()
            self.assertEqual(response.request_id, request_id)
            self.assertEqual(response.error.code, ErrorCode.CLOSED.value)
        self.assertEqual(self.worker._pending, {})

    async def test_register_and_unregister_pending(self) -> None:
        future = asyncio.get_running_loop().create_future()
        self.worker.register_pending(3, future)
        self.assertIs(self.worker.unregister_pending(3), future)
        self.assertIsNone(self.worker.unregister_pending(3))


class AthenaClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transport = Transport()
        self.worker = ClientWorker(self.transport)
        await self.worker.start()
        self.client = AthenaClient(self.transport, self.worker, owns_transport=False)

    async def asyncTearDown(self) -> None:
        await self.worker.stop()

    async def test_request_timeout_unregisters_pending_future(self) -> None:
        with self.assertRaises(asyncio.TimeoutError):
            await self.client.request("no/server", timeout=0.01)
        self.assertEqual(self.worker._pending, {})

    async def test_cancel_during_send_unregisters_pending_future(self) -> None:
        entered_send = asyncio.Event()

        async def blocked_send(*args, **kwargs):
            entered_send.set()
            await asyncio.Future()

        self.transport.send_request = blocked_send
        request = asyncio.create_task(self.client.request("blocked"))
        await entered_send.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(self.worker._pending, {})

    async def test_transport_eof_fails_pending_request_promptly(self) -> None:
        request = asyncio.create_task(self.client.request("waiting"))
        await eventually(lambda: bool(self.worker._pending))
        await self.transport.recv_client_message()
        await self.transport.aclose()
        with self.assertRaises(RpcException) as raised:
            await asyncio.wait_for(request, timeout=0.1)
        self.assertEqual(raised.exception.code, ErrorCode.CLOSED.value)
        self.assertEqual(self.worker._pending, {})

    async def test_respond_to_server_request_writes_reply(self) -> None:
        await self.client.respond_to_server_request("server:1", {"approved": True})
        reply = await self.transport.recv_client_message()
        self.assertIsInstance(reply, ServerRequestReply)
        self.assertEqual(reply.result, {"approved": True})

    async def test_fail_server_request_writes_internal_error(self) -> None:
        await self.client.fail_server_request("server:1", "rejected")
        reply = await self.transport.recv_client_message()
        self.assertEqual(reply.error_code, ErrorCode.INTERNAL.value)
        self.assertEqual(reply.error_message, "rejected")

    async def test_shutdown_sends_server_shutdown_request(self) -> None:
        await self.client.shutdown(timeout=0.01)
        request = self.transport._c2s.get_nowait()
        self.assertEqual(request.method, "server/shutdown")
