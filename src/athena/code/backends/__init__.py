"""LLM code generation backends (Codex, Qoder, DeepSeek)."""

from typing import Literal


class BackendUnavailableError(RuntimeError):
    pass


from athena.code.backends.codex import CodexBackend
from athena.code.backends.deepseek import DeepSeekCodeBackend
from athena.code.backends.qoder import QoderBackend


def load_backend(
    name: Literal["codex", "qoder", "deepseek"],
    **config: object,
):
    factories = {
        "codex": CodexBackend.from_config,
        "qoder": QoderBackend.from_config,
        "deepseek": lambda c: DeepSeekCodeBackend(**c),
    }
    return factories[name](config)


__all__ = [
    "BackendUnavailableError",
    "CodexBackend",
    "DeepSeekCodeBackend",
    "QoderBackend",
    "load_backend",
]
