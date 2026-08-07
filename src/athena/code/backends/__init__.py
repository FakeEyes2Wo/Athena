"""LLM code generation backends (Qoder, Codex)."""

from typing import Literal


class BackendUnavailableError(RuntimeError):
    pass


from athena.code.backends.codex import CodexBackend
from athena.code.backends.qoder import QoderBackend


def load_backend(
    name: Literal["codex", "qoder"],
    **config: object,
):
    factories = {
        "codex": CodexBackend.from_config,
        "qoder": QoderBackend.from_config,
    }
    return factories[name](config)


__all__ = [
    "BackendUnavailableError",
    "CodexBackend",
    "QoderBackend",
    "load_backend",
]
