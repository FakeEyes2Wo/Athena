"""Stable clarification domain errors."""


class ClarificationError(RuntimeError):
    """Reject an invalid clarification or confirmation operation."""

    def __init__(self, code: str, message: str) -> None:
        """Expose a stable transport code with readable context."""
        self.code = code
        super().__init__(f"{code}: {message}")


class ClarificationPersistenceError(RuntimeError):
    """Report unreadable or unwritable clarification data."""


__all__ = [
    "ClarificationError",
    "ClarificationPersistenceError",
]
