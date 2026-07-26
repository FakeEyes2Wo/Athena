import asyncio
import unittest

import pytest

from athena.app_server.lifecycle import AppServer
from athena.app_server.thread_manager import RuntimeThreadManager

from ._support import (
    cancel_event_waiters,
    force_stop_processor,
    immediate_runner,
)

OWNED_TASK_PREFIXES = (
    "client-worker",
    "fair-mux",
    "msg-processor-dispatch",
    "srv-request-",
    "merged-ctrl",
    "merged-sub",
    "submission-loop-",
    "turn-",
)


def owned_tasks() -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and task.get_name().startswith(OWNED_TASK_PREFIXES)
    ]


async def cleanup_app(app: AppServer) -> None:
    try:
        await asyncio.wait_for(app.shutdown(timeout=0.05), timeout=0.5)
    finally:
        await force_stop_processor(app.server)
        await cancel_event_waiters()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_completes_initialize_handshake(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        app = await AppServer.create(manager, owns_manager=True)
        try:
            self.assertEqual(app.server.state, "READY")
            self.assertTrue(app.transport._ready.is_set())
            self.assertFalse(app.transport.closed)
        finally:
            await cleanup_app(app)

    async def test_owned_manager_is_closed_by_shutdown(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        app = await AppServer.create(manager, owns_manager=True)
        await cleanup_app(app)
        self.assertEqual(manager.state, "closed")

    async def test_unowned_manager_remains_alive(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        app = await AppServer.create(manager, owns_manager=False)
        try:
            await cleanup_app(app)
            self.assertEqual(manager.state, "alive")
        finally:
            await manager.aclose("test_cleanup")

    async def test_nonpositive_capacity_is_clamped_by_transport(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        app = await AppServer.create(
            manager, control_capacity=0, event_capacity=0, owns_manager=True
        )
        try:
            self.assertEqual(app.transport._c2s.maxsize, 1)
            self.assertEqual(app.transport._s2c_event.maxsize, 1)
        finally:
            await cleanup_app(app)

    async def test_failed_initialize_rolls_back_started_components(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(
                AppServer.create(
                    manager,
                    protocol_version=999,
                    startup_timeout=0.05,
                    owns_manager=False,
                ),
                timeout=0.5,
            )
        await asyncio.sleep(0)
        try:
            self.assertEqual(owned_tasks(), [])
        finally:
            await manager.aclose("test_cleanup")

    async def test_normal_shutdown_leaves_no_owned_tasks(self) -> None:
        manager = RuntimeThreadManager(immediate_runner)
        app = await AppServer.create(manager, owns_manager=True)
        remaining: list[str] = []
        try:
            await asyncio.wait_for(app.shutdown(timeout=0.05), timeout=0.5)
            await asyncio.sleep(0)
            remaining = [task.get_name() for task in owned_tasks()]
        finally:
            await force_stop_processor(app.server)
            await cancel_event_waiters()
        self.assertEqual(remaining, [])
