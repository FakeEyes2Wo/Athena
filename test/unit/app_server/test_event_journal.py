import asyncio
import unittest

from pydantic import ValidationError

from athena.app_server.events import Event, EventJournal

from ._support import append_current_event


class EventJournalTests(unittest.IsolatedAsyncioTestCase):
    def test_initial_sequence_state(self) -> None:
        journal = EventJournal("thread:1")
        self.assertEqual(journal.next_sequence(), 1)
        self.assertEqual(journal.last_sequence, 0)
        self.assertEqual(len(journal), 0)

    async def test_append_under_condition_updates_tail(self) -> None:
        journal = EventJournal("thread:1")
        first = await append_current_event(journal, event_ref="artifact:1")
        second = await append_current_event(journal, event_ref="artifact:2")
        self.assertEqual((first.sequence, second.sequence), (1, 2))
        self.assertEqual(journal.last_sequence, 2)

    async def test_concurrent_callers_can_serialize_with_condition(self) -> None:
        journal = EventJournal("thread:1")
        events = await asyncio.gather(
            *(
                append_current_event(journal, event_ref=f"artifact:{index}")
                for index in range(100)
            )
        )
        self.assertEqual(
            sorted(event.sequence for event in events), list(range(1, 101))
        )

    async def test_read_from_zero_replays_all_records(self) -> None:
        journal = EventJournal("thread:1")
        for index in range(3):
            await append_current_event(journal, event_ref=f"artifact:{index}")
        iterator = journal.read_from(0)
        sequences = [await anext(iterator) for _ in range(3)]
        self.assertEqual([event.sequence for event in sequences], [1, 2, 3])

    async def test_read_from_cursor_starts_at_next_record(self) -> None:
        journal = EventJournal("thread:1")
        for index in range(3):
            await append_current_event(journal, event_ref=f"artifact:{index}")
        event = await anext(journal.read_from(2))
        self.assertEqual(event.sequence, 3)

    async def test_reader_waits_for_a_future_record(self) -> None:
        journal = EventJournal("thread:1")
        waiting = asyncio.create_task(anext(journal.read_from(0)))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        await append_current_event(journal, event_ref="artifact:later")
        event = await asyncio.wait_for(waiting, timeout=0.1)
        self.assertEqual(event.event_ref, "artifact:later")

    async def test_negative_cursor_is_treated_as_zero(self) -> None:
        journal = EventJournal("thread:1")
        await append_current_event(journal)
        event = await anext(journal.read_from(-10))
        self.assertEqual(event.sequence, 1)

    def test_event_is_frozen(self) -> None:
        event = Event(
            thread_id="thread:1",
            sequence=1,
            kind="item",
            event_ref="artifact:item",
        )
        with self.assertRaises(ValidationError):
            event.kind = "changed"
