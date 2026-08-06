"""Code domain types."""

from dataclasses import dataclass, field


@dataclass
class ExecutionOutput:
    """脚本执行的结果（stdout / stderr / 退出码 / 产物文件）。"""

    stdout: str
    stderr: str
    returncode: int
    files: list[str] = field(default_factory=list)


@dataclass
class GenerationResult:
    """Result of LLM code generation into a directory."""

    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    output: str = ""  # LLM's textual response


@dataclass
class EngineResult:
    """Aggregated result of the generate→execute→iterate loop."""

    rounds: int = 0
    final_output: ExecutionOutput | None = None
    files: list[str] = field(default_factory=list)
    success: bool = False


if __name__ == "__main__":
    exec_out = ExecutionOutput(stdout="ok", stderr="", returncode=0)
    gen_out = GenerationResult(files_created=["model.py"], output="code generated")
    result = EngineResult(
        rounds=1, final_output=exec_out, files=["model.py"], success=True
    )
    print(f"ExecutionOutput: {exec_out}")
    print(f"GenerationResult: {gen_out}")
    print(f"EngineResult: {result}")
