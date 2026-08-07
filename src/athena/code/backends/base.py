"""Abstract base for LLM code generation backends."""

import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path

from athena.code.types import ExecutionOutput, GenerationResult


def snapshot_files(target_dir: str | Path) -> dict[str, str]:
    """Return content hashes for regular files below a backend work directory."""
    root = Path(target_dir).resolve()
    snapshots: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        snapshots[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return snapshots


def generation_result(
    before: dict[str, str],
    after: dict[str, str],
    *,
    output: str,
) -> GenerationResult:
    """Build a generation result from observed filesystem changes."""
    return GenerationResult(
        files_created=sorted(after.keys() - before.keys()),
        files_modified=sorted(
            path for path in after.keys() & before.keys() if after[path] != before[path]
        ),
        files_deleted=sorted(before.keys() - after.keys()),
        output=output,
    )


def render_backend_prompt(
    prompt: str,
    previous_outputs: list[ExecutionOutput],
    history: list[dict],
) -> str:
    """Fold bounded execution feedback and round history into the next prompt."""
    parts = [prompt]
    recent_history = history[-3:]
    if recent_history:
        parts.append("Previous rounds (JSON):")
        parts.append(json.dumps(recent_history, ensure_ascii=False, default=str))
    for output in previous_outputs[-3:]:
        stdout = (output.stdout or "")[-4000:]
        stderr = (output.stderr or "")[-4000:]
        parts.append(
            f"Previous execution feedback:\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return "\n\n".join(parts)


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
