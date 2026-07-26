import asyncio
import unittest

from athena.app_server.submissions import InterruptTurn, StartTurn
from athena.app_server.thread_runtime import ThreadHandle, ThreadRuntime

from ._support import eventually


class RuntimeRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_interrupt_race_has_one_terminal_1000_times(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        for index in range(1000):
            started = asyncio.Event()
            release = asyncio.Event()

            async def runner(thread, turn, emit):
                del thread, turn, emit
                started.set()
                await release.wait()
                return "artifact:result", "artifact:next-context"

            runtime = ThreadRuntime(
                f"thread:{index}", "session:1", "artifact:context", runner
            )
            await runtime.start()
            handle = ThreadHandle(runtime)
            try:
                await handle.submit(StartTurn(f"turn:{index}", "artifact:request"))
                await asyncio.wait_for(started.wait(), timeout=0.1)
                interrupt = asyncio.create_task(
                    handle.submit(InterruptTurn(f"turn:{index}", "race"))
                )
                release.set()
                await asyncio.wait_for(interrupt, timeout=0.1)
                await eventually(lambda: runtime.state == "idle", timeout=0.1)
                terminal = [
                    event
                    for event in runtime.journal._records
                    if event.kind
                    in {"turn_completed", "turn_failed", "turn_interrupted"}
                ]
                self.assertEqual(len(terminal), 1, f"iteration {index}")
            finally:
                release.set()
                await runtime.force_close()

    async def test_different_thread_runners_execute_in_parallel(self) -> None:
        active = 0
        maximum = 0
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def runner(thread, turn, emit):
            nonlocal active, maximum
            del thread, turn, emit
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both_started.set()
            try:
                await release.wait()
            finally:
                active -= 1
            return "artifact:result", "artifact:next-context"

        runtimes = [
            ThreadRuntime(f"thread:{index}", "session:1", "artifact:context", runner)
            for index in range(2)
        ]
        for runtime in runtimes:
            await runtime.start()
        try:
            await asyncio.gather(
                *(
                    ThreadHandle(runtime).submit(
                        StartTurn(f"turn:{index}", "artifact:request")
                    )
                    for index, runtime in enumerate(runtimes)
                )
            )
            await asyncio.wait_for(both_started.wait(), timeout=0.1)
            self.assertEqual(maximum, 2)
        finally:
            release.set()
            for runtime in runtimes:
                await eventually(lambda runtime=runtime: runtime.state == "idle")
                await runtime.force_close()
