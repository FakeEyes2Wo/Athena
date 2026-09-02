"""Stable clarification domain errors."""


class ClarificationControllerError(RuntimeError):
    """Reject an invalid draft transition."""

    def __init__(self, code: str, message: str) -> None:
        """Expose a stable transport code with readable context."""
        self.code = code
        super().__init__(f"{code}: {message}")


class ClarificationPersistenceError(RuntimeError):
    """Report unreadable or unwritable clarification data."""


class ClarificationConfirmationError(RuntimeError):
    """Reject or fail a confirmation transaction."""

    def __init__(self, code: str, message: str) -> None:
        """Expose a stable transport code with readable context."""
        self.code = code
        super().__init__(f"{code}: {message}")


__all__ = [
    "ClarificationConfirmationError",
    "ClarificationControllerError",
    "ClarificationPersistenceError",
]
