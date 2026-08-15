"""Human request broker for the GUI gateway.

Bridges the supervisor's ``ask_user`` (``request_user_input`` tool) to the
frontend: ``ask`` blocks on an asyncio.Future, ``pending`` exposes the
outstanding questions, and ``reply`` resolves one by id.
"""

import asyncio
from uuid import uuid4


class HumanRequestBroker:
    """Hold outstanding supervisor questions and resolve them by request id."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}

    async def ask(self, prompt: str) -> str | None:
        """Block until the human replies to ``prompt``; returns the reply text."""
        request_id = uuid4().hex
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = (prompt, future)
        return await future

    def pending(self) -> list[dict]:
        """Return the outstanding questions in insertion order."""
        return [
            {"request_id": request_id, "prompt": prompt}
            for request_id, (prompt, _future) in self._pending.items()
        ]

    def reply(self, request_id: str, answer: str) -> bool:
        """Resolve an outstanding question; False when the id is unknown."""
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return False
        entry[1].set_result(answer)
        return True
