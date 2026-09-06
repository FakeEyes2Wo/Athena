import asyncio
import unittest

from athena.app_server.exceptions import OverloadedError
from athena.app_server.protocol import (
    ClientNotification,
    EventNotification,
    RequestEnvelope,
    ResponseEnvelope,
    ServerRequest,
)
from athena.app_server.transport import ServerRequestReply, Transport


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_nonpositive_capacities_are_clamped_to_one(self) -> None:
        transport = Transport(control_capacity=0, event_capacity=-1)
        self.assertEqual(transport._c2s.maxsize, 1)
        self.assertEqual(transport._s2c_event.maxsize, 1)

    async def test_request_round_trip(self) -> None:
        transport = Transport()
        request = RequestEnvelope(request_id=1, method="thread/start", params={})
        await transport.send_request(request)
        self.assertIs(await transport.recv_client_message(), request)

    async def test_full_request_lane_times_out_as_overloaded(self) -> None:
        transport = Transport(control_capacity=1)
        await transport.send_request(RequestEnvelope(request_id=1, method="one"))
        with self.assertRaises(OverloadedError):
            await transport.send_request(
                RequestEnvelope(request_id=2, method="two"), timeout=0.01
            )

    async def test_notification_is_lossy_when_command_lane_is_full(self) -> None:
        transport = Transport(control_capacity=1)
        request = RequestEnvelope(request_id=1, method="one")
        await transport.send_request(request)
        await transport.send_notification(ClientNotification(method="lossy"))
        self.assertIs(await transport.recv_client_message(), request)
        self.assertTrue(transport._c2s.empty())

    async def test_response_and_server_request_share_control_lane(self) -> None:
        transport = Transport()
        response = ResponseEnvelope(request_id=1, result={})
        reverse = ServerRequest(server_call_id="server:1", method="approval", params={})
        await transport.send_response(response)
        await transport.send_server_request(reverse)
        self.assertIs(await transport.recv_response_or_control(), response)
        self.assertIs(await transport.recv_response_or_control(), reverse)

    async def test_event_lane_is_independent(self) -> None:
        transport = Transport(control_capacity=1, event_capacity=1)
        event = EventNotification(
            subscription_id="sub:1",
            thread_id="thread:1",
            turn_id=None,
            sequence=1,
            kind="item",
            event_ref="artifact:item",
        )
        await transport.send_event(event)
        await transport.send_response(ResponseEnvelope(request_id=1, result={}))
        self.assertIs(await transport.recv_event(), event)
        self.assertIsInstance(
            await transport.recv_response_or_control(), ResponseEnvelope
        )

    async def test_server_request_reply_round_trip(self) -> None:
        transport = Transport()
        reply = ServerRequestReply("server:1", result={"approved": True})
        await transport.send_server_request_reply(reply)
        self.assertIs(await transport.recv_client_message(), reply)

    async def test_ready_barrier(self) -> None:
        transport = Transport()
        waiting = asyncio.create_task(transport.wait_ready())
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        transport.set_ready()
        await asyncio.wait_for(waiting, timeout=0.1)

    async def test_close_emits_eof_to_all_lanes(self) -> None:
        transport = Transport()
        await transport.aclose()
        self.assertTrue(transport.closed)
        self.assertIsNone(await transport.recv_client_message())
        self.assertIsNone(await transport.recv_response_or_control())
        self.assertIsNone(await transport.recv_event())

    async def test_close_is_idempotent(self) -> None:
        transport = Transport()
        await transport.aclose()
        await transport.aclose()
        self.assertTrue(transport.closed)

    async def test_full_lane_close_completes_after_consumer_drains(self) -> None:
        transport = Transport(control_capacity=1, event_capacity=1)
        request = RequestEnvelope(request_id=1, method="one")
        await transport.send_request(request)
        closing = asyncio.create_task(transport.aclose())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        self.assertIs(await transport.recv_client_message(), request)
        await asyncio.wait_for(closing, timeout=0.1)
        self.assertIsNone(await transport.recv_client_message())
