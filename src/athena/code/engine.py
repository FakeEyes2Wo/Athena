"""CodeEngine: generate code → execute → observe → iterate."""

import os
from pathlib import Path

from athena.code.backends.base import CodeBackend
from athena.code.monitor import AgentMonitor, WatchResult
from athena.code.output_specs import OutputSpec
from athena.code.runner import run_script
from athena.code.types import EngineResult, ExecutionOutput, GenerationResult


def _missing_outputs(target_dir: str, output_spec: OutputSpec | None) -> list[str]:
    if output_spec is None:
        return []
    return [
        path
        for path in output_spec.must_exist
        if not os.path.exists(os.path.join(target_dir, path))
    ]


def _review_failure(
    output: ExecutionOutput | None,
    missing: list[str],
) -> ExecutionOutput:
    message = f"missing required output: {', '.join(missing)}"
    if output is None:
        return ExecutionOutput(stdout="", stderr=message, returncode=-1)
    stderr = "\n".join(part for part in (output.stderr, message) if part)
    return ExecutionOutput(
        stdout=output.stdout,
        stderr=stderr,
        returncode=-1,
        files=output.files,
    )


class CodeEngine:
    """通用迭代循环：LLM generates code → subprocess executes → observe → iterate."""

    def __init__(self, backend: CodeBackend, monitor: AgentMonitor):
        self._backend = backend
        self._monitor = monitor

    async def run(
        self,
        *,
        prompt: str,
        target_dir: str,
        max_rounds: int = 3,
        output_spec: "OutputSpec | None" = None,
        entrypoint: str | None = None,
    ) -> EngineResult:
        """Run the generate→execute→iterate loop."""
        history: list[dict] = []
        previous_outputs: list[ExecutionOutput] = []
        all_files: list[str] = []
        round_num = 0

        for round_num in range(1, max_rounds + 1):
            # Stage 1: generate code and collect changed files
            gen_result = await self._backend.generate(
                prompt=prompt,
                target_dir=target_dir,
                previous_outputs=previous_outputs,
                history=history,
            )

            all_files.extend(gen_result.files_created)
            all_files.extend(gen_result.files_modified)

            script = (
                self._fixed_entrypoint(target_dir, entrypoint)
                if entrypoint is not None
                else self._find_main_script(target_dir, gen_result)
            )
            if script is None:
                # Stage 2a: model declared completion without a runnable script →
                # succeed only when outputs are present and prior rounds agree
                missing = _missing_outputs(target_dir, output_spec)
                done = "done" in gen_result.output.lower()
                prior_success = bool(
                    previous_outputs and previous_outputs[-1].returncode == 0
                )
                recovered_review = bool(
                    output_spec is not None
                    and previous_outputs
                    and "missing required output:" in previous_outputs[-1].stderr
                    and not missing
                )
                if (
                    done
                    and not missing
                    and (not previous_outputs or prior_success or recovered_review)
                ):
                    return EngineResult(
                        rounds=round_num,
                        final_output=ExecutionOutput(
                            stdout=gen_result.output,
                            stderr="",
                            returncode=0,
                            files=sorted(set(all_files)),
                        ),
                        files=sorted(set(all_files)),
                        success=True,
                    )
                if missing:
                    previous_outputs.append(_review_failure(None, missing))
                continue

            # Stage 2b: execute the script under watch and collect its output
            watch_result: WatchResult = await self._monitor.watch(
                run_script(script, cwd=target_dir),
                timeout_s=600,
            )

            exec_output: ExecutionOutput | None = (
                watch_result.result if watch_result.status == "OK" else None
            )
            if exec_output is None:
                exec_output = ExecutionOutput(
                    stdout="", stderr=watch_result.error, returncode=-1
                )

            missing = _missing_outputs(target_dir, output_spec)
            if exec_output.returncode == 0 and missing:
                exec_output = _review_failure(exec_output, missing)

            previous_outputs.append(exec_output)
            history.append(
                {
                    "round": round_num,
                    "files": gen_result.files_created + gen_result.files_modified,
                    "stdout": exec_output.stdout[-2000:],
                    "stderr": exec_output.stderr[-2000:],
                }
            )
            if exec_output.returncode == 0 and not missing:
                return EngineResult(
                    rounds=round_num,
                    final_output=exec_output,
                    files=sorted(set(all_files)),
                    success=True,
                )

        return EngineResult(
            rounds=round_num,
            final_output=previous_outputs[-1] if previous_outputs else None,
            files=sorted(set(all_files)),
            success=False,
        )

    @staticmethod
    def _fixed_entrypoint(target_dir: str, entrypoint: str) -> str | None:
        root = Path(target_dir).resolve()
        candidate = (root / entrypoint).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("entrypoint must stay inside target_dir")
        return str(candidate) if candidate.is_file() else None

    def _find_main_script(
        self,
        target_dir: str,
        gen_result: GenerationResult,
    ) -> str | None:
        """Find the first .py file to execute from generated files."""
        candidates = gen_result.files_created + gen_result.files_modified
        for f in candidates:
            if f.endswith(".py"):
                full = os.path.join(target_dir, f)
                if os.path.exists(full):
                    return full
        return None


if __name__ == "__main__":
    print("CodeEngine loaded (requires backend and monitor for full demo).")
    print(f"Engine class: {CodeEngine}")
