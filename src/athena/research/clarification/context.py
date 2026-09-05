"""Load and verify the confirmed task contract used by downstream prompts."""

from pathlib import Path
from typing import Any

from athena.core.contracts import ArtifactStore


class ConfirmedTaskContextError(RuntimeError):
    """Report missing, unconfirmed, or inconsistent confirmed-task context."""


class ConfirmedTaskContextProvider:
    """Verify the durable state, artifact, and named handoff as one contract."""

    def __init__(
        self,
        state: Any,
        store: ArtifactStore,
        state_root: str | Path,
    ) -> None:
        self._state = state
        self._store = store
        self._handoff_path = Path(state_root) / "handoffs" / "TASK_CLARIFICATION.md"

    @classmethod
    def from_runtime(cls, runtime: Any) -> "ConfirmedTaskContextProvider":
        """Bind the provider to the runtime's authoritative state paths."""
        return cls(runtime.state, runtime.store, runtime.config.paths.athena)

    async def load(self) -> str:
        """Return the model-visible contract after all projections agree."""
        # Require the confirmed structured projection and its artifact ref.
        if not self._state.task_understanding:
            raise ConfirmedTaskContextError("task understanding is not confirmed")
        handoff_ref = (self._state.handoff_refs or {}).get("task_clarification")
        if not handoff_ref:
            raise ConfirmedTaskContextError(
                "task clarification handoff is not confirmed"
            )

        # Load the immutable artifact at the external storage boundary.
        try:
            handoff_text = await self._store.get_text(handoff_ref)
        except Exception as error:
            raise ConfirmedTaskContextError(
                f"confirmed task handoff artifact is missing or unreadable: {error}"
            ) from error
        if not handoff_text.strip():
            raise ConfirmedTaskContextError("confirmed task handoff artifact is empty")

        # Require byte parity with the named handoff before any phase work.
        if not self._handoff_path.is_file():
            raise ConfirmedTaskContextError(
                f"named task handoff is missing: {self._handoff_path}"
            )
        if self._handoff_path.read_text(encoding="utf-8") != handoff_text:
            raise ConfirmedTaskContextError(
                "named task handoff does not match the confirmed artifact"
            )
        return _prompt_block(handoff_text)


def _prompt_block(handoff: str) -> str:
    return (
        "--- Confirmed task contract (authoritative) ---\n"
        f"{handoff.strip()}\n"
        "--- end of confirmed task contract ---"
    )


async def confirmed_task_context_block(runtime: Any) -> str:
    """Load confirmed context, allowing ungated legacy checkpoints to omit it."""
    try:
        return await ConfirmedTaskContextProvider.from_runtime(runtime).load()
    except ConfirmedTaskContextError as error:
        if (
            runtime.config.task_confirmation_gate
            or runtime.state.task_understanding is not None
        ):
            raise RuntimeError(
                "confirmed task handoff is missing or corrupt"
            ) from error
        return ""


def task_prompt(task: str, context_block: str) -> str:
    """Prepend the verified task contract to a model-visible task."""
    return f"{context_block}\n\n{task}".strip() if context_block else task


__all__ = [
    "ConfirmedTaskContextError",
    "ConfirmedTaskContextProvider",
    "confirmed_task_context_block",
    "task_prompt",
]
