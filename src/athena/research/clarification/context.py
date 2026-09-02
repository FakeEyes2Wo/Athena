"""Centralized confirmed task context loader and renderer.

This module is the only downstream loader for the confirmed task contract.
It deliberately reads the durable ``ResearchState`` projection + artifact
handoff, verifies the named handoff file still matches the content-addressed
artifact, and returns one immutable object used by PREPARE, evaluator,
Ideator, Plan, and VALIDATE prompts.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athena.core.contracts import ArtifactStore


class ConfirmedTaskContextError(RuntimeError):
    """Raised when confirmed task context is missing, unconfirmed, or corrupt."""


@dataclass(frozen=True)
class ConfirmedTaskContext:
    """Immutable confirmed task context shared by every downstream prompt."""

    original_task: str
    understanding: Mapping[str, object]
    handoff_text: str
    handoff_ref: str
    draft_id: str | None
    revision: int | None

    def render_prompt_block(self) -> str:
        """Render the exact model-visible confirmed task contract block."""
        handoff = self.handoff_text.strip()
        if not handoff:
            raise ConfirmedTaskContextError(
                "confirmed task handoff is empty; cannot render prompt block"
            )
        return (
            "--- Confirmed task contract (authoritative) ---\n"
            f"{handoff}\n"
            "--- end of confirmed task contract ---"
        )


class ConfirmedTaskContextProvider:
    """Load the confirmed task context with integrity checks."""

    def __init__(
        self,
        state: Any,
        store: ArtifactStore,
        state_root: str | Path,
    ) -> None:
        """Bind confirmed state, artifacts, and the named handoff directory."""
        self._state = state
        self._store = store
        self._state_root = Path(state_root)

    @property
    def _handoff_path(self) -> Path:
        return self._state_root / "handoffs" / "TASK_CLARIFICATION.md"

    @classmethod
    def from_runtime(cls, runtime: Any) -> "ConfirmedTaskContextProvider":
        """Build a provider from the stable runtime configuration surface."""
        config = getattr(runtime, "config", None)
        state_root = config.paths.athena if config is not None else None
        if state_root is None:
            # Legacy runtime fixtures expose only the former session-root field.
            state_root = getattr(runtime, "_athena", None)
        if state_root is None:
            raise ConfirmedTaskContextError("confirmed task state root is unavailable")
        return cls(runtime.state, runtime.store, state_root)

    @staticmethod
    def _state_metadata(state: Any) -> tuple[str | None, int | None]:
        """Read draft metadata from state attrs or task_understanding mapping."""
        if state is None:
            return None, None
        draft_id = next(
            (
                value
                for value in (
                    getattr(state, "task_clarification_draft_id", None),
                    getattr(state, "clarification_draft_id", None),
                )
                if value is not None
            ),
            None,
        )
        revision = next(
            (
                value
                for value in (
                    getattr(state, "task_clarification_revision", None),
                    getattr(state, "clarification_revision", None),
                )
                if value is not None
            ),
            None,
        )
        understanding = getattr(state, "task_understanding", None)
        if isinstance(understanding, Mapping):
            draft_id = (
                draft_id if draft_id is not None else understanding.get("draft_id")
            )
            revision = (
                revision if revision is not None else understanding.get("revision")
            )
        return draft_id, revision

    def _file_metadata(self) -> tuple[str | None, int | None]:
        """Read metadata from clarification.json when state doesn't carry it."""
        clarification_path = self._state_root / "clarification.json"
        if not clarification_path.is_file():
            return None, None
        try:
            data = json.loads(clarification_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Legacy or partially written clarification metadata is unavailable.
            return None, None
        return data.get("draft_id"), data.get("revision")

    def _metadata(self) -> tuple[str | None, int | None]:
        """Return draft metadata from state, understanding, then draft file."""
        draft_id, revision = self._state_metadata(self._state)
        if draft_id is None or revision is None:
            file_id, file_revision = self._file_metadata()
            draft_id = draft_id if draft_id is not None else file_id
            revision = revision if revision is not None else file_revision
        return (
            str(draft_id) if draft_id is not None else None,
            int(revision) if revision is not None else None,
        )

    async def load(self) -> ConfirmedTaskContext:
        """Load confirmed state, named handoff, and artifact; require parity."""
        # Phase 1: require the confirmed structured projection and its artifact ref.
        if self._state is None:
            raise ConfirmedTaskContextError("confirmed task state is unavailable")
        if self._store is None:
            raise ConfirmedTaskContextError("artifact store is unavailable")

        understanding = getattr(self._state, "task_understanding", None)
        if not understanding:
            raise ConfirmedTaskContextError("task understanding is not confirmed")

        handoff_refs = getattr(self._state, "handoff_refs", None) or {}
        handoff_ref = handoff_refs.get("task_clarification")
        if not handoff_ref:
            raise ConfirmedTaskContextError(
                "task clarification handoff is not confirmed"
            )

        # Phase 2: load the immutable artifact at the external artifact boundary.
        try:
            handoff_text = await self._store.get_text(handoff_ref)
        except Exception as exc:
            # Artifact stores may be local or remote; normalize their read failures.
            raise ConfirmedTaskContextError(
                f"confirmed task handoff artifact is missing or unreadable: {exc}"
            ) from exc
        if not handoff_text.strip():
            raise ConfirmedTaskContextError("confirmed task handoff artifact is empty")

        # Phase 3: require byte parity with the named handoff before any phase work.
        if not self._handoff_path.is_file():
            raise ConfirmedTaskContextError(
                f"named task handoff is missing: {self._handoff_path}"
            )
        file_text = self._handoff_path.read_text(encoding="utf-8")
        if file_text != handoff_text:
            raise ConfirmedTaskContextError(
                "named task handoff does not match the confirmed artifact"
            )

        # Phase 4: assemble the single immutable context shared downstream.
        original_task = (
            getattr(self._state, "task_text", None)
            or getattr(self._state, "original_task", None)
            or ""
        )
        draft_id, revision = self._metadata()
        return ConfirmedTaskContext(
            original_task=str(original_task),
            understanding=dict(understanding),
            handoff_text=handoff_text,
            handoff_ref=str(handoff_ref),
            draft_id=draft_id,
            revision=revision,
        )


def task_context_block(context: ConfirmedTaskContext | None) -> str:
    """Render a prompt block for a loaded context, or an empty string."""
    if context is None:
        return ""
    return context.render_prompt_block()


async def confirmed_task_context_block(runtime: Any) -> str:
    """Load confirmed context, allowing ungated legacy checkpoints to omit it."""
    try:
        context = await ConfirmedTaskContextProvider.from_runtime(runtime).load()
    except ConfirmedTaskContextError as exc:
        state = getattr(runtime, "state", None)
        confirmed = getattr(state, "task_understanding", None) is not None
        gated = bool(getattr(runtime, "task_confirmation_gate", False))
        if confirmed or gated:
            raise RuntimeError("confirmed task handoff is missing or corrupt") from exc
        return ""
    return context.render_prompt_block()


def task_prompt(task: str, context_block: str) -> str:
    """Prepend the verified task contract to a model-visible task."""
    return f"{context_block}\n\n{task}".strip() if context_block else task


__all__ = [
    "ConfirmedTaskContext",
    "ConfirmedTaskContextError",
    "ConfirmedTaskContextProvider",
    "confirmed_task_context_block",
    "task_context_block",
    "task_prompt",
]
