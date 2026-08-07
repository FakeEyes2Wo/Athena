"""Execute a planned experiment inside an isolated Git worktree."""

import asyncio
from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

from pydantic import BaseModel

from athena.code.backends.base import CodeBackend
from athena.code.engine import CodeEngine
from athena.code.monitor import AgentMonitor
from athena.code.output_specs import OutputSpec
from athena.core.workspace import GitWorkBranch
from athena.core.contracts import ArtifactRef
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.evaluation.types import EvalResult, EvalSpec

EXPERIMENT_ENTRYPOINT = "run_experiment.py"
EVALUATION_ENTRYPOINT = "eval.py"


class ProcessResult(BaseModel):
    """Observed result of one experiment subprocess."""

    returncode: int
    output: str


class CodeExecutionError(RuntimeError):
    """Experiment execution failed after producing log evidence."""

    def __init__(self, message: str, *, logs: ArtifactRef):
        super().__init__(message)
        self.logs = logs


async def _run_python(
    script: str,
    cwd: Path,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run one Python entrypoint and retain its exit status and output."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        script,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_s
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return ProcessResult(
            returncode=-1,
            output=f"Timed out after {timeout_s:g}s: {script}",
        )
    return ProcessResult(
        returncode=process.returncode or 0,
        output="\n".join(
            part.decode(errors="replace") for part in (stdout, stderr) if part
        ),
    )


class CodegenResult(BaseModel):
    """Successful code generation and evaluation evidence."""

    experiment_id: str
    commit: str
    diff: ArtifactRef
    eval: EvalResult
    logs: ArtifactRef
    wall_time_s: float = 0.0


class CodeRouter:
    """Choose the configured code-generation backend for a hypothesis."""

    def route(self, hypothesis: Hypothesis) -> str:
        normalized = hypothesis.intervention.lower().replace(" ", "_")
        if "from_scratch" in normalized:
            return "codex"
        if len(hypothesis.intervention) > 200:
            return "codex"
        return "qoder"


class CodeAgent:
    """Run generated experiment and protected evaluation entrypoints."""

    def __init__(
        self,
        backend: str = "auto",
        *,
        backends: Mapping[str, CodeBackend] | None = None,
        workspace=None,
        max_rounds: int = 3,
        monitor: AgentMonitor | None = None,
    ) -> None:
        self.backend = backend
        self._router = CodeRouter()
        self._backends = dict(backends or {})
        self._workspace = workspace
        self._max_rounds = max_rounds
        self._monitor = monitor or AgentMonitor()

    async def execute(
        self,
        experiment_id: str,
        hypothesis: Hypothesis,
        plan: ExperimentPlan,
        parent_commit: str,
        eval_spec: EvalSpec,
        worktree: GitWorkBranch,
    ) -> CodegenResult:
        wt_path = Path(worktree.path)
        wt_path.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        log_path = wt_path / f"athena_logs_{experiment_id}.txt"
        logs_ref = f"artifact://{log_path}"

        await self._ensure_evaluator(wt_path, eval_spec)
        protected_before = self._protected_hashes(wt_path)

        generation_log: ProcessResult | None = None
        if self._backends:
            backend_name = (
                self._router.route(hypothesis)
                if self.backend == "auto"
                else self.backend
            )
            try:
                selected_backend = self._backends[backend_name]
            except KeyError as exc:
                raise CodeExecutionError(
                    f"code backend is not configured: {backend_name}", logs=logs_ref
                ) from exc
            engine_result = await CodeEngine(selected_backend, self._monitor).run(
                prompt=self._generation_prompt(
                    experiment_id, hypothesis, plan, eval_spec
                ),
                target_dir=str(wt_path),
                max_rounds=self._max_rounds,
                output_spec=OutputSpec(must_exist=[EXPERIMENT_ENTRYPOINT]),
                entrypoint=EXPERIMENT_ENTRYPOINT,
            )
            final_output = engine_result.final_output
            generation_log = ProcessResult(
                returncode=(final_output.returncode if final_output else -1),
                output=(
                    "\n".join(
                        part
                        for part in (
                            final_output.stdout if final_output else "",
                            final_output.stderr if final_output else "",
                        )
                        if part
                    )
                    or "code generation produced no execution output"
                ),
            )
            await self._write_logs(log_path, [("generation", generation_log)])
            if not engine_result.success:
                raise CodeExecutionError(
                    "code generation failed after bounded revision rounds",
                    logs=logs_ref,
                )
            changed = set(engine_result.files)
            if EXPERIMENT_ENTRYPOINT not in changed:
                raise CodeExecutionError(
                    "code backend did not modify run_experiment.py", logs=logs_ref
                )
            unexpected = changed - {EXPERIMENT_ENTRYPOINT}
            if unexpected:
                raise CodeExecutionError(
                    f"code backend changed files outside scope: {sorted(unexpected)}",
                    logs=logs_ref,
                )
            self._verify_protected_files(wt_path, protected_before, logs_ref)
        else:
            await self._ensure_experiment_entrypoint(
                wt_path, experiment_id, hypothesis, plan, eval_spec
            )

        process_results: list[tuple[str, ProcessResult]] = []
        if generation_log is not None:
            process_results.append(("generation", generation_log))
        run_env = {**os.environ, "ATHENA_EXPERIMENT_ID": experiment_id}
        for script, timeout_s in (
            (EXPERIMENT_ENTRYPOINT, 300),
            (EVALUATION_ENTRYPOINT, 60),
        ):
            process_result = await _run_python(script, wt_path, timeout_s, env=run_env)
            process_results.append((script, process_result))
            await self._write_logs(log_path, process_results)
            if process_result.returncode != 0:
                message = (
                    process_result.output
                    if process_result.returncode == -1
                    else f"{script} exited with code {process_result.returncode}"
                )
                raise CodeExecutionError(message, logs=logs_ref)

        evaluation = await self._load_evaluation(wt_path, experiment_id, logs_ref)
        diff_ref = f"artifact://diffs/{experiment_id}"
        commit = parent_commit
        if self._workspace is not None:
            diff_ref = await self._workspace.diff(worktree)
            commit = await self._workspace.commit(
                worktree,
                diff_ref,
                f"experiment: {experiment_id}",
            )
        return CodegenResult(
            experiment_id=experiment_id,
            commit=commit,
            diff=diff_ref,
            eval=evaluation,
            logs=logs_ref,
            wall_time_s=time.time() - started_at,
        )

    @staticmethod
    def _generation_prompt(
        experiment_id: str,
        hypothesis: Hypothesis,
        plan: ExperimentPlan,
        eval_spec: EvalSpec,
    ) -> str:
        context = json.dumps(
            {
                "experiment_id": experiment_id,
                "hypothesis": hypothesis.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "eval_spec": eval_spec.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return (
            "Implement this Athena experiment by creating or revising only "
            "run_experiment.py. Do not modify eval.py, eval_spec.json, "
            "splits.json, or any other file. Read splits.json for the frozen "
            "dataset paths. The script must write predictions.csv and labels.csv "
            "with one value column each. Do not run eval.py.\n\n"
            f"Frozen experiment context:\n{context}"
        )

    @staticmethod
    def _protected_hashes(root: Path) -> dict[str, str | None]:
        protected = (EVALUATION_ENTRYPOINT, "eval_spec.json", "splits.json")
        return {
            name: (
                hashlib.sha256((root / name).read_bytes()).hexdigest()
                if (root / name).is_file()
                else None
            )
            for name in protected
        }

    @classmethod
    def _verify_protected_files(
        cls,
        root: Path,
        expected: dict[str, str | None],
        logs_ref: ArtifactRef,
    ) -> None:
        observed = cls._protected_hashes(root)
        changed = sorted(name for name in expected if expected[name] != observed[name])
        if changed:
            raise CodeExecutionError(
                f"protected file changed: {', '.join(changed)}", logs=logs_ref
            )

    @staticmethod
    async def _ensure_evaluator(wt_path: Path, eval_spec: EvalSpec) -> None:
        entrypoint = wt_path / EVALUATION_ENTRYPOINT
        if entrypoint.is_file():
            return
        from athena.evaluation.factory import default_eval_script

        evaluator = eval_spec.eval_script or default_eval_script(eval_spec)
        await asyncio.to_thread(
            entrypoint.write_text,
            evaluator,
            encoding="utf-8",
        )

    @staticmethod
    async def _ensure_experiment_entrypoint(
        wt_path: Path,
        experiment_id: str,
        hypothesis: Hypothesis,
        plan: ExperimentPlan,
        eval_spec: EvalSpec,
    ) -> None:
        entrypoint = wt_path / EXPERIMENT_ENTRYPOINT
        if entrypoint.is_file():
            return
        context = json.dumps(
            {
                "experiment_id": experiment_id,
                "hypothesis": hypothesis.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "eval_spec": eval_spec.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        source = (
            f"EXPERIMENT_CONTEXT = {context!r}\n"
            'raise RuntimeError("code generation backend did not produce '
            'run_experiment.py")\n'
        )
        await asyncio.to_thread(entrypoint.write_text, source, encoding="utf-8")

    @staticmethod
    async def _write_logs(
        log_path: Path, results: list[tuple[str, ProcessResult]]
    ) -> None:
        rendered = "\n\n".join(
            f"== {script} ==\n{result.output}" for script, result in results
        )
        await asyncio.to_thread(log_path.write_text, rendered, encoding="utf-8")

    @staticmethod
    async def _load_evaluation(
        wt_path: Path, experiment_id: str, logs_ref: ArtifactRef
    ) -> EvalResult:
        result_path = wt_path / "eval_result.json"
        if not result_path.is_file():
            raise CodeExecutionError(
                "evaluation did not produce eval_result.json", logs=logs_ref
            )
        samples_path = wt_path / "predictions.csv"
        if not samples_path.is_file():
            raise CodeExecutionError(
                "evaluation did not produce per-sample predictions", logs=logs_ref
            )
        try:
            payload = json.loads(
                await asyncio.to_thread(result_path.read_text, encoding="utf-8")
            )
            evaluated_id = payload.get("experiment_id")
            if evaluated_id != experiment_id:
                raise ValueError(f"evaluation experiment id mismatch: {evaluated_id!r}")
            result = EvalResult.model_validate(
                {
                    **payload,
                    "per_sample": f"artifact://{samples_path}",
                }
            )
            if not math.isfinite(result.primary):
                raise ValueError("evaluation primary metric must be finite")
            return result
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise CodeExecutionError(
                f"invalid evaluation output: {exc}", logs=logs_ref
            ) from exc
