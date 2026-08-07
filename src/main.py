"""Runnable Athena PREPARE -> SEARCH -> VALIDATE -> REPORT composition root."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic_ai import Agent as PydanticAgent

from athena.code.backends.base import CodeBackend
from athena.code.backends.codex import CodexBackend
from athena.code.backends.qoder import QoderBackend
from athena.code.execution import LocalExperimentRuntime
from athena.evaluation.trusted import TrustedEvaluator
from athena.evaluation.types import EvaluationInputs
from athena.git_workspace import LocalGitWorkspace
from athena.ideator import Ideator
from athena.research import (
    ResearchMethod,
    ResearchRuntime,
    ResearchWorkflowDependencies,
)
from athena.research.budget import RunMode
from athena.storage.artifact_store import LocalArtifactStore
from athena.workflows.prepare.baseline import create_baseline
from athena.workflows.prepare.runtime import (
    PreparedWorkflowData,
    prepare_workflow_data,
)
from athena.workflows.report import Reporter
from athena.workflows.search.code_agent import CodeAgent
from athena.workflows.search.search_loop import SearchLoop
from athena.workflows.validate import Validator

BackendName = Literal["codex", "qoder", "auto"]


@dataclass(frozen=True)
class RunConfig:
    """Validated command-line configuration for one Athena run."""

    data: Path
    target: str
    model: str
    backend: BackendName
    output_dir: Path
    task_type: str = "classification"
    data_type: str = "tabular"
    metric: str | None = None
    direction: Literal["maximize", "minimize"] | None = None
    description: str = ""
    max_experiments: int = 3
    max_no_improve: int = 2
    code_model: str | None = None
    hil: bool = False
    debug: bool = False


@dataclass
class WorkflowContext:
    """Application-owned dependencies populated during PREPARE."""

    prepared: PreparedWorkflowData | None = None
    base_commit: str | None = None


@dataclass(frozen=True)
class Application:
    runtime: ResearchRuntime
    context: WorkflowContext


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run Athena's complete PREPARE, SEARCH, VALIDATE, and REPORT workflow."
        )
    )
    parser.add_argument("--data", required=True, type=Path, help="Input CSV dataset")
    parser.add_argument("--target", required=True, help="Target column name")
    parser.add_argument(
        "--model",
        default=os.environ.get("ATHENA_IDEATOR_MODEL", ""),
        help="PydanticAI model identifier used by Ideator and Reporter",
    )
    parser.add_argument(
        "--backend",
        choices=("codex", "qoder", "auto"),
        default="auto",
        help="Experiment code-generation backend",
    )
    parser.add_argument(
        "--code-model",
        default=None,
        help="Optional backend-specific code model override",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".athena/run"),
        help="Run state, artifacts, worktrees, and report directory",
    )
    parser.add_argument("--task-type", default="classification")
    parser.add_argument("--data-type", default="tabular")
    parser.add_argument("--metric", default=None)
    parser.add_argument("--direction", choices=("maximize", "minimize"), default=None)
    parser.add_argument("--description", default="")
    parser.add_argument("--max-experiments", type=int, default=3)
    parser.add_argument("--max-no-improve", type=int, default=2)
    parser.add_argument("--hil", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser


def parse_args(argv: list[str] | None = None) -> RunConfig:
    namespace = _parser().parse_args(argv)
    return RunConfig(
        data=namespace.data,
        target=namespace.target,
        model=namespace.model,
        backend=namespace.backend,
        output_dir=namespace.output_dir,
        task_type=namespace.task_type,
        data_type=namespace.data_type,
        metric=namespace.metric,
        direction=namespace.direction,
        description=namespace.description,
        max_experiments=namespace.max_experiments,
        max_no_improve=namespace.max_no_improve,
        code_model=namespace.code_model,
        hil=namespace.hil,
        debug=namespace.debug,
    )


def validate_config(config: RunConfig) -> None:
    if not config.data.is_file():
        raise ValueError(f"dataset does not exist: {config.data}")
    if config.data.suffix.lower() != ".csv":
        raise ValueError("--data must point to a CSV file")
    if not config.target.strip():
        raise ValueError("--target must be non-empty")
    if not config.model.strip():
        raise ValueError("--model or ATHENA_IDEATOR_MODEL is required")
    if config.max_experiments <= 0 or config.max_no_improve <= 0:
        raise ValueError("experiment budgets must be positive")
    if (
        config.output_dir.exists()
        and (config.output_dir / "experiment_repo" / ".git").exists()
    ):
        raise ValueError(
            "output directory already contains an experiment repository; "
            "choose a new --output-dir"
        )


def _metric(config: RunConfig) -> tuple[str, Literal["maximize", "minimize"]]:
    if config.metric:
        metric = config.metric
    elif config.task_type == "regression":
        metric = "rmse"
    elif config.task_type == "binary_classification":
        metric = "roc_auc"
    else:
        metric = "f1_macro"
    direction = config.direction or (
        "minimize" if metric in {"rmse", "mae", "mse"} else "maximize"
    )
    return metric, direction


def _build_backends(config: RunConfig) -> dict[str, CodeBackend]:
    backends: dict[str, CodeBackend] = {}
    if config.backend in {"codex", "auto"}:
        executable = shutil.which("codex")
        if executable is None:
            raise ValueError("Codex CLI is required but was not found on PATH")
        backends["codex"] = CodexBackend.from_config(
            {"executable": executable, "model": config.code_model}
        )
    if config.backend in {"qoder", "auto"}:
        backends["qoder"] = QoderBackend.from_config({"model": config.code_model})
    return backends


async def _git(repo: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )
    return stdout.decode("utf-8", errors="replace").strip()


async def _seed_repository(
    workspace: LocalGitWorkspace,
    repo: Path,
    prepared: PreparedWorkflowData,
) -> str:
    ignore = "\n".join(
        (
            "predictions.csv",
            "labels.csv",
            "fold_ids.csv",
            "eval_result.json",
            "athena_logs_*.txt",
            ".athena/",
            "__pycache__/",
            "",
        )
    )
    await workspace.init(
        repo_path=repo,
        initial_file=".gitignore",
        initial_content=ignore,
    )
    shutil.copyfile(prepared.evaluator_path, repo / "eval.py")
    shutil.copyfile(prepared.eval_spec_path, repo / "eval_spec.json")
    await _git(repo, "add", "eval.py", "eval_spec.json")
    await _git(repo, "commit", "-m", "freeze evaluation protocol")
    return await _git(repo, "rev-parse", "HEAD")


class _DeferredValidator:
    def __init__(
        self,
        context: WorkflowContext,
        workspace: LocalGitWorkspace,
        code_agent: CodeAgent,
        validation_inputs: EvaluationInputs | None = None,
        test_inputs: EvaluationInputs | None = None,
    ) -> None:
        self._context = context
        self._workspace = workspace
        self._code_agent = code_agent
        self._validation_inputs = validation_inputs
        self._test_inputs = test_inputs

    def set_inputs(
        self, validation_inputs: EvaluationInputs, test_inputs: EvaluationInputs
    ) -> None:
        self._validation_inputs = validation_inputs
        self._test_inputs = test_inputs

    async def run(self, sota_id, tree):
        prepared = self._context.prepared
        if prepared is None:
            raise RuntimeError("validation requested before PREPARE completed")
        if self._validation_inputs is None or self._test_inputs is None:
            raise RuntimeError("validation inputs are not configured")
        return await Validator(
            self._workspace,
            self._code_agent,
            prepared.eval_spec,
            self._validation_inputs,
            self._test_inputs,
        ).run(sota_id, tree)


def build_application(
    config: RunConfig,
    *,
    ideator=None,
    backends: dict[str, CodeBackend] | None = None,
    reporter=None,
) -> Application:
    output = config.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifacts = LocalArtifactStore(output / "artifacts" / "objects")
    repo = output / "experiment_repo"
    worktree_root = output / "worktrees"
    workspace = LocalGitWorkspace(repo, worktree_root, artifacts.put_bytes)
    selected_backends = backends if backends is not None else _build_backends(config)
    runtime = LocalExperimentRuntime()
    evaluator = TrustedEvaluator(artifacts)
    code_agent = CodeAgent(
        backend=config.backend,
        backends=selected_backends,
        workspace=workspace,
        runtime=runtime,
        artifacts=artifacts,
        evaluator=evaluator,
    )
    context = WorkflowContext()
    active_ideator = ideator or Ideator(
        agent_factory=lambda _role, _index: PydanticAgent(config.model),
        artifacts=artifacts,
    )
    active_reporter = reporter or Reporter(
        agent=PydanticAgent(config.model),
        output_dir=output / "reports",
    )

    deferred_validator = _DeferredValidator(context, workspace, code_agent)

    async def prepare_baseline(task, tree):
        prepared = await prepare_workflow_data(
            config.data,
            target=config.target,
            task=task,
            output_dir=output,
        )
        context.prepared = prepared
        context.base_commit = await _seed_repository(workspace, repo, prepared)
        deferred_validator.set_inputs(prepared.validation_inputs, prepared.test_inputs)
        return await create_baseline(
            workspace,
            context.base_commit,
            tree,
            data_profile=prepared.profile,
            processing_log=prepared.processing_log,
            eval_spec=prepared.eval_spec,
            validation_inputs=prepared.validation_inputs,
            code_agent=code_agent,
        )

    def search_factory(tree, budget, checkpoint):
        prepared = context.prepared
        if prepared is None:
            raise RuntimeError("SEARCH requested before PREPARE completed")
        return SearchLoop(
            tree,
            workspace,
            prepared.eval_spec,
            budget,
            RunMode(hil=config.hil, debug=config.debug),
            data_profile=prepared.profile,
            validation_inputs=prepared.validation_inputs,
            ideator=active_ideator,
            code_agent=code_agent,
            before_iteration=checkpoint,
        )

    dependencies = ResearchWorkflowDependencies(
        prepare_baseline=prepare_baseline,
        search_factory=search_factory,
        validator=deferred_validator,
        reporter=active_reporter,
    )
    runtime = ResearchRuntime(
        save_path=output / "research_tree.json",
        dependencies=dependencies,
    )

    async def emit(kind: str, data: dict[str, object]) -> None:
        if kind == "phase/change":
            print(f"[athena] phase={data['phase']}", file=sys.stderr, flush=True)

    runtime.subscribe(emit)
    return Application(runtime=runtime, context=context)


async def drive_runtime(runtime: ResearchRuntime, config: RunConfig) -> dict[str, Any]:
    """Drive all canonical phases and persist state at every boundary."""
    output = config.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    tree_path = output / "research_tree.json"
    metric, direction = _metric(config)

    async def save_tree() -> None:
        await runtime.dispatch(ResearchMethod.TREE_SAVE, {"path": str(tree_path)})

    try:
        await runtime.dispatch(
            ResearchMethod.TASK_CONFIGURE,
            {
                "task_type": config.task_type,
                "data_type": config.data_type,
                "target_vars": [config.target],
                "primary_metric": metric,
                "direction": direction,
                "description": config.description,
            },
        )
        await runtime.dispatch(
            ResearchMethod.SEARCH_START,
            {
                "max_experiments": config.max_experiments,
                "max_no_improve": config.max_no_improve,
            },
        )
        active = runtime.run_task
        if active is None:
            raise RuntimeError("SEARCH did not create a workflow task")
        await active
        if runtime.phase != "COMPLETED":
            raise RuntimeError(f"SEARCH ended in phase {runtime.phase}")
        await save_tree()
        validation = await runtime.dispatch(ResearchMethod.VALIDATE_START, {})
        await save_tree()
        report = await runtime.dispatch(ResearchMethod.REPORT_GENERATE, {})
        await save_tree()
        return {**report, "validation": validation, "tree_path": str(tree_path)}
    except BaseException:
        try:
            await save_tree()
        except Exception as save_error:
            print(
                f"[athena] failed to persist research tree: {save_error}",
                file=sys.stderr,
            )
        raise


async def run(config: RunConfig) -> dict[str, Any]:
    application = build_application(config)
    try:
        result = await drive_runtime(application.runtime, config)
        summary = {
            "phase": application.runtime.phase,
            "sota_id": application.runtime.tree.best_experiment_id(),
            "tree_path": result["tree_path"],
            "report_ref": result["report_ref"],
            "backend": config.backend,
            "budget": application.runtime.budget.model_dump(mode="json"),
        }
        summary_path = config.output_dir.resolve() / "run_summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return summary
    finally:
        await application.runtime.aclose()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    config = parse_args(argv)
    try:
        validate_config(config)
    except (OSError, ValueError) as exc:
        print(f"athena: invalid configuration: {exc}", file=sys.stderr)
        return 2
    try:
        summary = asyncio.run(run(config))
    except KeyboardInterrupt:
        print("athena: interrupted", file=sys.stderr)
        return 130
    except BaseException as exc:
        print(f"athena: workflow failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
