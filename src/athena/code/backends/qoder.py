"""Optional Qoder code-generation adapter."""

import importlib
import inspect

from athena.code.backends import BackendUnavailableError
from athena.code.backends.base import CodeBackend
from athena.code.types import ExecutionOutput, GenerationResult


class QoderBackend(CodeBackend):
    def __init__(self, client: object):
        self._client = client

    @classmethod
    def from_config(cls, config: dict[str, object]) -> "QoderBackend":
        try:
            sdk = importlib.import_module("qoder_agent_sdk")
        except ModuleNotFoundError as error:
            raise BackendUnavailableError("qoder backend unavailable") from error
        factory = getattr(sdk, "Client", None)
        return cls(factory(**config) if callable(factory) else sdk)

    async def generate(
        self,
        prompt: str,
        target_dir: str,
        previous_outputs: list[ExecutionOutput],
        history: list[dict],
    ) -> GenerationResult:
        result = self._client.generate(
            prompt=prompt,
            target_dir=target_dir,
            previous_outputs=previous_outputs,
            history=history,
        )
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, GenerationResult) else GenerationResult(**result)
