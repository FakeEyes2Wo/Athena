"""Human request broker for the GUI gateway.

Bridges the supervisor's ``ask_user`` (``request_user_input`` tool) to the
frontend: typed ``ask`` blocks on an asyncio.Future, ``pending`` exposes the
outstanding questions, and ``reply`` resolves one by id.

The broker is session-scoped. Every request carries an explicit ``session_id``
and ``scope_id``, and every settlement decision happens under one asyncio lock.
A bounded ledger of recently settled request IDs lets duplicate replies return a
typed ``duplicate_reply`` error instead of ``unknown_request``.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from athena.core.human_request import (
    HumanChoice,
    HumanContractError,
    HumanOutcome,
    HumanReply,
    HumanRequest,
)
from athena.core.contracts import new_id

_SETTLED_LEDGER_SIZE = 256


@dataclass(frozen=True)
class PendingRequest:
    request: HumanRequest
    future: asyncio.Future[HumanOutcome]


@dataclass(frozen=True)
class ActiveScope:
    """The session/scope legacy ``ask`` calls are bound to."""

    session_id: str
    scope_id: str | None
    scope_kind: str


class HumanRequestBroker:
    """Hold outstanding supervisor questions and resolve them by request id."""

    def __init__(self, clock: Any = None) -> None:
        self._pending: dict[str, PendingRequest] = {}
        self._lock = asyncio.Lock()
        self._settled_ids: deque[str] = deque(maxlen=_SETTLED_LEDGER_SIZE)
        self._active_scope = ActiveScope("default", None, "runtime")
        self._clock = clock or (lambda: datetime.now(UTC))

    def bind(self, session_id: str, scope_id: str, scope_kind: str) -> None:
        """Bind the broker to the active session/scope for legacy asks."""
        self._active_scope = ActiveScope(session_id, scope_id, scope_kind)

    @property
    def active_session_id(self) -> str:
        return self._active_scope.session_id

    # ── typed ask / legacy ask ───────────────────────────────────────────

    async def ask(
        self,
        prompt_or_request: HumanRequest | str,
        *,
        choices: list[dict[str, str]] | list[HumanChoice] | None = None,
        allow_custom: bool = True,
        allow_skip: bool = True,
        timeout_s: float = 120.0,
    ) -> HumanOutcome | str | None:
        """Block until the human replies; typed requests return ``HumanOutcome``.

        The legacy string signature is retained for older callers and returns the
        old string result (``choice:<value>``, ``<text>``, ``skip``).
        """
        if isinstance(prompt_or_request, HumanRequest):
            return await self._ask_typed(prompt_or_request)

        request = self._build_legacy_request(
            prompt_or_request,
            choices=choices or [],
            allow_custom=allow_custom,
            allow_skip=allow_skip,
            timeout_s=timeout_s,
        )
        outcome = await self._ask_typed(request)
        if outcome.kind == "choice":
            return f"choice:{outcome.value}"
        if outcome.kind == "skip":
            return "skip"
        if outcome.kind == "timeout":
            return "skip"
        if outcome.kind == "cancelled":
            return None
        return outcome.value or ""

    def _build_legacy_request(
        self,
        prompt: str,
        *,
        choices: list[dict[str, str]] | list[HumanChoice],
        allow_custom: bool,
        allow_skip: bool,
        timeout_s: float,
    ) -> HumanRequest:
        now = self._clock()
        request_id = new_id("human")
        return HumanRequest(
            request_id=request_id,
            session_id=self._active_scope.session_id,
            scope_id=self._active_scope.scope_id or "runtime",
            scope_kind=self._active_scope.scope_kind,  # type: ignore[arg-type]
            prompt=prompt,
            choices=[HumanChoice.model_validate(choice) for choice in choices],
            allow_custom=allow_custom,
            allow_skip=allow_skip,
            created_at=now,
            expires_at=now + timedelta(seconds=max(1.0, timeout_s)),
        )

    async def _ask_typed(self, request: HumanRequest) -> HumanOutcome:
        async with self._lock:
            if (
                request.request_id in self._pending
                or request.request_id in self._settled_ids
            ):
                raise HumanContractError(
                    "duplicate_request", "request id already exists in this broker"
                )
            future: asyncio.Future[HumanOutcome] = (
                asyncio.get_running_loop().create_future()
            )
            self._pending[request.request_id] = PendingRequest(request, future)

        timeout = max(0.0, (request.expires_at - self._clock()).total_seconds())
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            async with self._lock:
                return self._settle_timeout_locked(request.request_id)

    # ── settlement API ───────────────────────────────────────────────────

    async def reply(
        self,
        session_id: str,
        request_id: str,
        reply: HumanReply,
    ) -> HumanOutcome:
        """Resolve an outstanding request with one typed reply."""
        async with self._lock:
            return self._settle_reply_locked(session_id, request_id, reply)

    async def pending(self, session_id: str | None = None) -> list[HumanRequest]:
        """Return outstanding requests for one session in insertion order."""
        async with self._lock:
            session = session_id or self._active_session_id
            return [
                item.request
                for item in self._pending.values()
                if item.request.session_id == session
            ]

    async def cancel_scope(self, session_id: str, scope_id: str) -> list[HumanOutcome]:
        """Cancel all outstanding requests in one session scope."""
        async with self._lock:
            return self._cancel_matching_locked(
                lambda req: req.session_id == session_id and req.scope_id == scope_id
            )

    async def cancel_session(self, session_id: str) -> list[HumanOutcome]:
        """Cancel every outstanding request belonging to a session."""
        async with self._lock:
            return self._cancel_matching_locked(
                lambda req: req.session_id == session_id
            )

    # ── locked helpers ───────────────────────────────────────────────────

    def _settle_reply_locked(
        self, session_id: str, request_id: str, reply: HumanReply
    ) -> HumanOutcome:
        if request_id in self._settled_ids:
            raise HumanContractError("duplicate_reply", "request was already settled")
        entry = self._pending.get(request_id)
        if entry is None:
            raise HumanContractError("unknown_request", "no such pending request")
        request = entry.request
        if request.session_id != session_id:
            raise HumanContractError(
                "cross_session", "request belongs to a different session"
            )
        if self._clock() > request.expires_at:
            raise HumanContractError(
                "expired_request", "request is no longer accepting replies"
            )

        if reply.kind == "choice":
            match = next(
                (choice for choice in request.choices if choice.value == reply.value),
                None,
            )
            if match is None:
                raise HumanContractError(
                    "invalid_choice", "choice value is not in the request"
                )
            outcome = HumanOutcome(
                request_id=request_id,
                kind="choice",
                value=reply.value,
                choice_label=match.label,
                settled_at=self._clock(),
            )
        elif reply.kind == "text":
            if not request.allow_custom:
                raise HumanContractError(
                    "custom_not_allowed", "this request does not accept free text"
                )
            outcome = HumanOutcome(
                request_id=request_id,
                kind="text",
                value=reply.text,
                settled_at=self._clock(),
            )
        else:
            if not request.allow_skip:
                raise HumanContractError(
                    "skip_not_allowed", "this request does not accept skip"
                )
            outcome = HumanOutcome(
                request_id=request_id,
                kind="skip",
                settled_at=self._clock(),
            )

        self._remove_pending_locked(request_id)
        entry.future.set_result(outcome)
        return outcome

    def _settle_timeout_locked(self, request_id: str) -> HumanOutcome:
        entry = self._pending.get(request_id)
        if entry is None:
            raise HumanContractError("unknown_request", "no such pending request")
        outcome = HumanOutcome(
            request_id=request_id,
            kind="timeout",
            settled_at=self._clock(),
        )
        self._remove_pending_locked(request_id)
        entry.future.set_result(outcome)
        return outcome

    def _cancel_matching_locked(
        self, predicate: Callable[[HumanRequest], bool]
    ) -> list[HumanOutcome]:
        outcomes: list[HumanOutcome] = []
        for request_id in list(self._pending):
            entry = self._pending[request_id]
            if predicate(entry.request):
                outcome = HumanOutcome(
                    request_id=request_id,
                    kind="cancelled",
                    settled_at=self._clock(),
                )
                self._remove_pending_locked(request_id)
                entry.future.set_result(outcome)
                outcomes.append(outcome)
        return outcomes

    def _remove_pending_locked(self, request_id: str) -> None:
        self._pending.pop(request_id, None)
        self._settled_ids.append(request_id)
