"""Execute a planned experiment inside an isolated Git worktree."""

import asyncio
from collections.abc import Mapping
import hashlib
import json
import shutil
from pathlib import Path
import time

from pydantic import BaseModel

from athena.code.backends.base import CodeBackend, render_backend_prompt
from athena.code.execution import ExecutionRequest, LocalExperimentRuntime
from athena.code.file_policy import GeneratedTreePolicy
from athena.code.monitor import AgentMonitor
from athena.code.review import review_diff
from athena.code.types import ExecutionOutput
from athena.core.workspace import GitWorkBranch
from athena.core.contracts import ArtifactRef
from athena.core.research_models import ExperimentPlan, Hypothesis
from athena.evaluation.types import EvalResult, EvalSpec, EvaluationInputs

EXPERIMENT_ENTRYPOINT = "run_experiment.py"
EVALUATION_ENTRYPOINT = "eval.py"


def _installed_dependencies() -> set[str]:
    """Top-level import names available in the frozen Athena environment."""
    try:
        from importlib.metadata import packages_distributions
    except ImportError:  # pragma: no cover - Python 3.11+
        return set()
    names = set()
    for top_level in packages_distributions().values():
        names.update(top_level)
    return names


class ProcessResult(BaseModel):
    """Observed result of one experiment subprocess."""

    returncode: int
    output: str


class CodeExecutionError(RuntimeError):
    """Experiment execution failed after producing log evidence."""

    def __init__(self, message: str, *, logs: ArtifactRef | None = None):
        super().__init__(message)
        self.logs = logs


class CodegenResult(BaseModel):
    """Successful code generation and evaluation evidence."""

    experiment_id: str
    commit: str
    diff: ArtifactRef
    eval: EvalResult
    evaluation: ArtifactRef
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
    """Run a bounded generate → execute → evaluate → repair loop."""

    def __init__(
        self,
        backend: str = "auto",
        *,
        backends: Mapping[str, CodeBackend] | None = None,
        workspace=None,
        runtime=None,
        artifacts=None,
        evaluator=None,
        max_rounds: int = 3,
        monitor: AgentMonitor | None = None,
    ) -> None:
        self.backend = backend
        self._router = CodeRouter()
        self._backends = dict(backends or {})
        self._workspace = workspace
        self._runtime = runtime or LocalExperimentRuntime()
        self._artifacts = artifacts
        self._evaluator = evaluator
        self._max_rounds = max_rounds
        self._monitor = monitor or AgentMonitor()

    async def execute(
        self,
        experiment_id,
        hypothesis,
        plan,
        parent_commit,
        eval_spec,
        worktree,
        *,
        inputs: EvaluationInputs,
    ) -> CodegenResult:
        wt_path = Path(worktree.path)
        wt_path.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        log_path = wt_path / f"athena_logs_{experiment_id}.txt"
        logs_ref = f"artifact://{log_path}"

        await self._stage_inputs(wt_path, inputs)
        await self._ensure_evaluator(wt_path, eval_spec)
        protected_before = self._protected_hashes(wt_path)

        history: list[dict] = []
        previous_outputs: list[ExecutionOutput] = []
        log_results: list[tuple[str, ProcessResult]] = []
        evaluation: EvalResult | None = None
        if self._artifacts is None or self._evaluator is None:
            raise CodeExecutionError(
                "trusted evaluation is not configured", logs=logs_ref
            )
        if self._backends:
            for round_num in range(1, self._max_rounds + 1):
                backend_name = (
                    self._router.route(hypothesis)
                    if self.backend == "auto"
                    else self.backend
                )
                try:
                    selected_backend = self._backends[backend_name]
                except KeyError as exc:
                    raise CodeExecutionError(
                        f"code backend is not configured: {backend_name}",
                        logs=logs_ref,
                    ) from exc
                generation = await selected_backend.generate(
                    prompt=render_backend_prompt(
                        self._generation_prompt(
                            experiment_id, hypothesis, plan, eval_spec
                        ),
                        previous_outputs,
                        history,
                    ),
                    target_dir=str(wt_path),
                    previous_outputs=previous_outputs,
                    history=history,
                )
                changed = (
                    set(generation.files_created)
                    | set(generation.files_modified)
                    | set(generation.files_deleted)
                )
                self._verify_protected_files(wt_path, protected_before, logs_ref)
                if EXPERIMENT_ENTRYPOINT not in changed:
                    raise CodeExecutionError(
                        "code backend did not modify run_experiment.py",
                        logs=logs_ref,
                    )
                try:
                    GeneratedTreePolicy().validate(wt_path, changed)
                except CodeExecutionError as policy_error:
                    raise CodeExecutionError(
                        str(policy_error), logs=logs_ref
                    ) from policy_error
                run_output = await self._runtime.run(
                    ExecutionRequest(
                        entrypoint=EXPERIMENT_ENTRYPOINT,
                        cwd=wt_path,
                        timeout_s=300,
                        environment={
                            "ATHENA_EXPERIMENT_ID": experiment_id,
                            "ATHENA_PHASE": inputs.phase,
                        },
                        readonly_inputs=(inputs.features_path,),
                    )
                )
                run_record = (
                    "run_experiment.py",
                    ProcessResult(
                        returncode=run_output.returncode,
                        output="\n".join(
                            part
                            for part in (run_output.stdout, run_output.stderr)
                            if part
                        ),
                    ),
                )
                log_results.append(run_record)
                await self._write_logs(log_path, log_results)
                history.append(
                    {
                        "round": round_num,
                        "files": sorted(changed),
                        "stdout": (run_output.stdout or "")[-2000:],
                        "stderr": (run_output.stderr or "")[-2000:],
                    }
                )
                if run_output.returncode != 0:
                    previous_outputs.append(run_output)
                    continue
                try:
                    evaluation = await self._evaluator.evaluate(
                        experiment_id,
                        wt_path / "predictions.csv",
                        inputs,
                        eval_spec,
                    )
                except Exception as exc:
                    previous_outputs.append(
                        ExecutionOutput(
                            stdout="",
                            stderr=f"trusted evaluation failed: {exc}",
                            returncode=-1,
                        )
                    )
                    continue
                break
            else:
                raise CodeExecutionError(
                    "code generation failed after bounded revision rounds",
                    logs=logs_ref,
                )
        else:
            raise CodeExecutionError(
                "no code-generation backend configured for trusted execution",
                logs=logs_ref,
            )
        if evaluation is None:
            raise CodeExecutionError(
                "experiment did not produce a valid trusted evaluation",
                logs=logs_ref,
            )

        predictions_path = wt_path / "predictions.csv"
        canonical = await asyncio.to_thread(lambda: predictions_path.read_bytes())
        evaluation_ref = await self._artifacts.put_bytes(canonical)
        diff_ref = f"artifact://diffs/{experiment_id}"
        commit = parent_commit
        if self._workspace is not None:
            diff = await self._workspace.diff(worktree)
            patch = await self._artifacts.get_bytes(diff.ref)
            verdict = review_diff(
                patch.decode("utf-8", errors="replace"),
                allowed_files=set(diff.paths),
                declared_dependencies=_installed_dependencies(),
            )
            if verdict.action == "reject":
                raise CodeExecutionError(
                    "generated diff rejected: " + "; ".join(verdict.reasons),
                    logs=logs_ref,
                )
            diff_ref = diff.ref
            commit = await self._workspace.commit(
                worktree, diff, f"experiment: {experiment_id}"
            )
        log_text = await asyncio.to_thread(log_path.read_text, encoding="utf-8")
        logs_ref = await self._artifacts.put_text(log_text)
        return CodegenResult(
            experiment_id=experiment_id,
            commit=commit,
            diff=diff_ref,
            eval=evaluation,
            evaluation=evaluation_ref,
            logs=logs_ref,
            wall_time_s=time.time() - started_at,
        )

    async def execute_frozen(
        self,
        experiment_id,
        hypothesis,
        plan,
        parent_commit,
        eval_spec,
        worktree,
        *,
        inputs: EvaluationInputs,
    ) -> CodegenResult:
        wt_path = Path(worktree.path)
        wt_path.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        log_path = wt_path / f"athena_logs_{experiment_id}.txt"
        logs_ref = f"artifact://{log_path}"

        await self._stage_inputs(wt_path, inputs)
        await self._ensure_evaluator(wt_path, eval_spec)
        if self._artifacts is None or self._evaluator is None:
            raise CodeExecutionError(
                "trusted evaluation is not configured", logs=logs_ref
            )

        run_output = await self._runtime.run(
            ExecutionRequest(
                entrypoint=EXPERIMENT_ENTRYPOINT,
                cwd=wt_path,
                timeout_s=300,
                environment={
                    "ATHENA_EXPERIMENT_ID": experiment_id,
                    "ATHENA_PHASE": inputs.phase,
                },
                readonly_inputs=(inputs.features_path,),
            )
        )
        await self._write_logs(
            log_path,
            [
                (
                    "run_experiment.py",
                    ProcessResult(
                        returncode=run_output.returncode,
                        output="\n".join(
                            part
                            for part in (run_output.stdout, run_output.stderr)
                            if part
                        ),
                    ),
                )
            ],
        )
        if run_output.returncode != 0:
            raise CodeExecutionError(
                "frozen final-test execution failed", logs=logs_ref
            )

        evaluation = await self._evaluator.evaluate(
            experiment_id, wt_path / "predictions.csv", inputs, eval_spec
        )
        evaluation_ref = await self._artifacts.put_bytes(
            await asyncio.to_thread((wt_path / "predictions.csv").read_bytes)
        )
        diff_ref = f"artifact://diffs/{experiment_id}"
        commit = parent_commit
        if self._workspace is not None:
            diff = await self._workspace.diff(worktree)
            diff_ref = diff.ref
            commit = await self._workspace.commit(
                worktree, diff, f"experiment: {experiment_id}"
            )
        return CodegenResult(
            experiment_id=experiment_id,
            commit=commit,
            diff=diff_ref,
            eval=evaluation,
            evaluation=evaluation_ref,
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
        protected = (
            EVALUATION_ENTRYPOINT,
            "eval_spec.json",
            ".athena/phase_manifest.json",
        )
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
    async def _stage_inputs(wt_path: Path, inputs: EvaluationInputs) -> None:
        stage = wt_path / ".athena" / "inputs"
        stage.mkdir(parents=True, exist_ok=True)
        for name in ("train", "predict"):
            source = inputs.train_path if name == "train" else inputs.features_path
            await asyncio.to_thread(shutil.copyfile, source, stage / f"{name}.csv")
        manifest = {
            "phase": inputs.phase,
            "row_id_column": inputs.row_id_column,
            "target": inputs.target,
            "train": ".athena/inputs/train.csv",
            "predict": ".athena/inputs/predict.csv",
        }
        await asyncio.to_thread(
            (wt_path / ".athena" / "phase_manifest.json").write_text,
            json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )

    @staticmethod
    async def _write_logs(
        log_path: Path, results: list[tuple[str, ProcessResult]]
    ) -> None:
        rendered = "\n\n".join(
            f"== {script} ==\n{result.output}" for script, result in results
        )
        await asyncio.to_thread(log_path.write_text, rendered, encoding="utf-8")
