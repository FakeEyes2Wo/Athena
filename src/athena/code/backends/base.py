"""Abstract base for LLM code generation backends."""

from abc import ABC, abstractmethod
from athena.code.types import ExecutionOutput, GenerationResult


class CodeBackend(ABC):
    """LLM code generation backend. Generates files into target_dir."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        target_dir: str,
        previous_outputs: list[ExecutionOutput],
        history: list[dict],
    ) -> GenerationResult:
        """Generate code files in target_dir. Returns what changed."""
        ...
