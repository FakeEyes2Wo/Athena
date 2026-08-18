"""Human request broker for the GUI gateway.

Bridges the supervisor's ``ask_user`` (``request_user_input`` tool) to the
frontend: ``ask`` blocks on an asyncio.Future, ``pending`` exposes the
outstanding questions, and ``reply`` resolves one by id.

A request may carry multiple-choice options. The reply is normalized as:
- ``choice:<value>`` for option selection
- ``<text>`` for a free-text answer (legacy ``answer`` field)
- ``skip`` for skipping the question
"""

import asyncio
from uuid import uuid4


class HumanRequestBroker:
    """Hold outstanding supervisor questions and resolve them by request id."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[dict, asyncio.Future]] = {}

    async def ask(
        self,
        prompt: str,
        *,
        choices: list[dict] | None = None,
        allow_custom: bool = True,
        allow_skip: bool = True,
        timeout_s: float = 120.0,
    ) -> str | None:
        """Block until the human replies to ``prompt``; timeout behaves as skip."""
        request_id = uuid4().hex
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = (
            {
                "request_id": request_id,
                "prompt": prompt,
                "choices": choices,
                "allow_custom": allow_custom,
                "allow_skip": allow_skip,
            },
            future,
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            return "skip"
        finally:
            # 取消/超时也要清理，否则前端稍后 human_reply 会命中已结束的 Future。
            self._pending.pop(request_id, None)

    def pending(self) -> list[dict]:
        """Return the outstanding questions in insertion order."""
        return [
            dict(request)
            for request, _future in self._pending.values()
        ]

    def reply(
        self,
        request_id: str,
        answer: str | None = None,
        *,
        choice: str | None = None,
        skip: bool = False,
    ) -> bool:
        """Resolve an outstanding question; False when unknown or already settled."""
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return False
        _request, future = entry
        if future.done():
            return False
        if skip:
            future.set_result("skip")
        elif choice is not None:
            future.set_result(f"choice:{choice}")
        elif answer is not None:
            future.set_result(answer)
        else:
            future.set_result("skip")
        return True
