import asyncio
import unittest

import pytest

from athena.app_server.events import Event
from athena.app_server.events import FairMux, Subscription
from athena.app_server.lifecycle import (
    AppServer,
    SubscriptionRegistry,
    ThreadEventHandlerRegistry,
)
from athena.app_server.protocol import EventNotification, Method
from athena.app_server.thread_manager import RuntimeThreadManager
from athena.app_server.transport import Transport

from ._support import cancel_event_waiters, force_stop_processor, immediate_runner


def make_event(thread_id: str, sequence: int) -> Event:
    return Event(
        thread_id=thread_id,
        turn_id="turn:1",
        sequence=sequence,
        kind="item",
        event_ref=f"artifact:{thread_id}:{sequence}",
    )


class SubscriptionTests(unittest.TestCase):
    def test_defaults_match_current_queue_shape(self) -> None:
        subscription = Subscription("sub:1", "thread:1")
        self.assertEqual(subscription.cursor, 0)
        self.assertEqual(subscription.queue.maxsize, 64)
        self.assertTrue(subscription.active)
        self.assertIsNone(subscription.pump_task)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_registry_tracks_current_placeholder(self) -> None:
        registry = ThreadEventHandlerRegistry()
        await registry.attach("thread:1")
        self.assertEqual(registry._handlers, {"thread:1": None})
        await registry.stop_all()
        self.assertEqual(registry._handlers, {})

    async def test_subscription_registry_adds_and_removes_mux_entry(self) -> None:
        transport = Transport()
        mux = FairMux(transport.send_event)
        registry = SubscriptionRegistry(mux)
        subscription = await registry.create("thread:1", after_sequence=4)
        self.assertEqual(subscription.thread_id, "thread:1")
        self.assertEqual(subscription.cursor, 4)
        self.assertIs(mux._subs[subscription.subscription_id], subscription)
        await registry.remove(subscription.subscription_id)
        self.assertFalse(subscription.active)
        self.assertNotIn(subscription.subscription_id, mux._subs)

    async def test_registry_accepts_current_unvalidated_thread_and_cursor(self) -> None:
        mux = FairMux(lambda notification: None)
        registry = SubscriptionRegistry(mux)
        subscription = await registry.create("thread:missing", after_sequence=999)
        self.assertEqual(subscription.thread_id, "thread:missing")
        self.assertEqual(subscription.cursor, 999)

    async def test_same_cursor_gets_unique_subscription_ids(self) -> None:
        mux = FairMux(lambda notification: None)
        registry = SubscriptionRegistry(mux)
        first = await registry.create("thread:1", after_sequence=0)
        second = await registry.create("thread:1", after_sequence=0)
        self.assertNotEqual(first.subscription_id, second.subscription_id)


class FairMuxTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_is_mapped_to_notification(self) -> None:
        sent: list[EventNotification] = []
        delivered = asyncio.Event()

        async def send(notification: EventNotification) -> None:
            sent.append(notification)
            delivered.set()

        mux = FairMux(send)
        subscription = Subscription("sub:1", "thread:1")
        subscription.queue.put_nowait(make_event("thread:1", 1))
        subscription.has_data.set()
        mux.add(subscription)
        await mux.start()
        try:
            await asyncio.wait_for(delivered.wait(), timeout=0.1)
        finally:
            await mux.stop()
            await cancel_event_waiters()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].subscription_id, "sub:1")
        self.assertEqual(sent[0].event_ref, "artifact:thread:1:1")

    async def test_all_ready_subscriptions_are_eventually_drained(self) -> None:
        sent: list[str] = []
        done = asyncio.Event()

        async def send(notification: EventNotification) -> None:
            sent.append(notification.subscription_id)
            if len(sent) == 4:
                done.set()

        mux = FairMux(send)
        first = Subscription("a", "thread:a")
        second = Subscription("b", "thread:b")
        for sequence in range(1, 3):
            first.queue.put_nowait(make_event("thread:a", sequence))
            second.queue.put_nowait(make_event("thread:b", sequence))
        first.has_data.set()
        second.has_data.set()
        mux.add(first)
        mux.add(second)
        await mux.start()
        try:
            await asyncio.wait_for(done.wait(), timeout=0.1)
        finally:
            await mux.stop()
            await cancel_event_waiters()
        self.assertEqual(sorted(sent), ["a", "a", "b", "b"])

    async def test_busy_subscription_does_not_starve_another(self) -> None:
        sent: list[str] = []
        saw_second = asyncio.Event()
        first = Subscription("a", "thread:a")
        second = Subscription("b", "thread:b")

        async def send(notification: EventNotification) -> None:
            sent.append(notification.subscription_id)
            if notification.subscription_id == "a" and len(sent) < 20:
                first.queue.put_nowait(make_event("thread:a", len(sent) + 1))
                first.has_data.set()
            if notification.subscription_id == "b":
                saw_second.set()

        first.queue.put_nowait(make_event("thread:a", 1))
        second.queue.put_nowait(make_event("thread:b", 1))
        first.has_data.set()
        second.has_data.set()
        mux = FairMux(send)
        mux.add(first)
        mux.add(second)
        await mux.start()
        try:
            await asyncio.wait_for(saw_second.wait(), timeout=0.05)
        finally:
            await mux.stop()
            await cancel_event_waiters()
        self.assertIn("b", sent[:8])

    async def test_idle_mux_waits_without_polling(self) -> None:
        import athena.app_server.events as events_module

        calls = 0
        real_sleep = asyncio.sleep

        async def counted_sleep(delay: float) -> None:
            nonlocal calls
            calls += 1
            await real_sleep(max(delay, 0.001))

        original = events_module.asyncio.sleep
        events_module.asyncio.sleep = counted_sleep
        mux = FairMux(lambda notification: None)
        try:
            await mux.start()
            await real_sleep(0.02)
        finally:
            await mux.stop()
            events_module.asyncio.sleep = original
            await cancel_event_waiters()
        self.assertEqual(calls, 0)


class SubscriptionPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = RuntimeThreadManager(immediate_runner)
        self.app = await AppServer.create(self.manager, owns_manager=True)

    async def asyncTearDown(self) -> None:
        try:
            await asyncio.wait_for(self.app.shutdown(timeout=0.05), timeout=0.5)
        finally:
            await force_stop_processor(self.app.server)
            await cancel_event_waiters()

    async def test_subscribed_runtime_event_reaches_client(self) -> None:
        thread = await self.app.client.request(
            Method.THREAD_START,
            {"session_id": "session:1", "context_ref": "artifact:context"},
            timeout=0.1,
        )
        await self.app.client.request(
            Method.THREAD_SUBSCRIBE,
            {"thread_id": thread["thread_id"], "after_sequence": 0},
            timeout=0.1,
        )
        await self.app.client.request(
            Method.TURN_START,
            {"thread_id": thread["thread_id"], "request_ref": "artifact:request"},
            timeout=0.1,
        )
        event = await self.app.client.next_event(timeout=0.1)
        self.assertIsNotNone(event)
        self.assertEqual(event.thread_id, thread["thread_id"])
