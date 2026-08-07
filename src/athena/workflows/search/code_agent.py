"""Execute a planned experiment inside an isolated Git worktree."""

import asyncio
import json
import math
from pathlib import Path
import sys
import time

from pydantic import BaseModel

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


async def _run_python(script: str, cwd: Path, timeout_s: float) -> ProcessResult:
    """Run one Python entrypoint and retain its exit status and output."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        script,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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

    def __init__(self, backend: str = "auto"):
        self.backend = backend
        self._router = CodeRouter()

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

        await self._write_evaluator(wt_path, experiment_id)
        await self._ensure_experiment_entrypoint(
            wt_path, experiment_id, hypothesis, plan, eval_spec
        )

        process_results: list[tuple[str, ProcessResult]] = []
        for script, timeout_s in (
            (EXPERIMENT_ENTRYPOINT, 300),
            (EVALUATION_ENTRYPOINT, 60),
        ):
            process_result = await _run_python(script, wt_path, timeout_s)
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
        return CodegenResult(
            experiment_id=experiment_id,
            commit=parent_commit,
            diff=f"artifact://diffs/{experiment_id}",
            eval=evaluation,
            logs=logs_ref,
            wall_time_s=time.time() - started_at,
        )

    @staticmethod
    async def _write_evaluator(wt_path: Path, experiment_id: str) -> None:
        evaluator = f"""import json
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score

predictions = pd.read_csv("predictions.csv")
labels = pd.read_csv("labels.csv")
y_pred = predictions.iloc[:, 0]
y_true = labels.iloc[:, 0]
primary = float(f1_score(y_true, y_pred, average="macro"))
secondary = {{"accuracy": float(accuracy_score(y_true, y_pred))}}
with open("eval_result.json", "w", encoding="utf-8") as stream:
    json.dump({{
        "experiment_id": "{experiment_id}",
        "primary": primary,
        "secondary": secondary,
    }}, stream, indent=2)
"""
        await asyncio.to_thread(
            (wt_path / EVALUATION_ENTRYPOINT).write_text,
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
