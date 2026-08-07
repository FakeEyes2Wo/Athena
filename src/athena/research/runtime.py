"""Authoritative ResearchTree v2 workflow lifecycle outside app-server."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from athena.core.research_tree import ExperimentStatus, ResearchTree
from athena.research.budget import BudgetSnapshot
from athena.research.models import MetricSpec, TaskMetaData


class ResearchMethod:
    PARSE_INTENT = "PARSE_INTENT"
    TASK_CONFIGURE = "TASK_CONFIGURE"
    SEARCH_START = "SEARCH_START"
    SEARCH_PAUSE = "SEARCH_PAUSE"
    SEARCH_RESUME = "SEARCH_RESUME"
    SEARCH_STOP = "SEARCH_STOP"
    VALIDATE_START = "VALIDATE_START"
    REPORT_GENERATE = "REPORT_GENERATE"
    TREE_GET = "tree_get"
    TREE_SAVE = "tree_save"
    TREE_LOAD = "tree_load"
    ALL = frozenset(
        {
            PARSE_INTENT,
            TASK_CONFIGURE,
            SEARCH_START,
            SEARCH_PAUSE,
            SEARCH_RESUME,
            SEARCH_STOP,
            VALIDATE_START,
            REPORT_GENERATE,
            TREE_GET,
            TREE_SAVE,
            TREE_LOAD,
        }
    )


ResearchPhase = Literal[
    "IDLE",
    "CONFIGURED",
    "PREPARE",
    "SEARCH",
    "PAUSED",
    "VALIDATE",
    "REPORT",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]
EmitFn = Callable[[str, dict[str, object]], Awaitable[None] | None]
PrepareBaselineFn = Callable[[TaskMetaData, ResearchTree], Awaitable[str]]
SearchFactory = Callable[
    [ResearchTree, BudgetSnapshot, Callable[[], Awaitable[Any]]], Any
]


@dataclass(frozen=True)
class ResearchWorkflowDependencies:
    prepare_baseline: PrepareBaselineFn | None = None
    search_factory: SearchFactory | None = None
    validator: Any | None = None
    reporter: Any | None = None


class ResearchRuntime:
    """Own research state, workflow tasks, cooperative pause, and events."""

    def __init__(
        self,
        *,
        tree: ResearchTree | None = None,
        save_path: str | Path = ".athena/research_tree.json",
        dependencies: ResearchWorkflowDependencies | None = None,
    ) -> None:
        self._tree = tree or ResearchTree()
        self._phase: ResearchPhase = "IDLE"
        self._active_task: TaskMetaData | None = None
        self._budget = BudgetSnapshot()
        self._run_task: asyncio.Task[None] | None = None
        self._resume_gate = asyncio.Event()
        self._resume_gate.set()
        self._subscribers: dict[str, EmitFn] = {}
        self._save_path = Path(save_path)
        self._dependencies = dependencies or ResearchWorkflowDependencies()

    @property
    def phase(self) -> ResearchPhase:
        return self._phase

    @property
    def run_task(self) -> asyncio.Task[None] | None:
        return self._run_task

    @property
    def tree(self) -> ResearchTree:
        return self._tree

    @property
    def budget(self) -> BudgetSnapshot:
        return self._budget

    def subscribe(self, emit: EmitFn) -> str:
        subscription_id = f"research:{uuid4().hex}"
        self._subscribers[subscription_id] = emit
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        self._subscribers.pop(subscription_id, None)

    async def _publish(self, kind: str, data: dict[str, object]) -> None:
        async def invoke(subscription_id: str, emit: EmitFn) -> str | None:
            try:
                result = emit(kind, data)
                if asyncio.iscoroutine(result):
                    await result
                return None
            except Exception:
                return subscription_id

        failed = await asyncio.gather(
            *(
                invoke(subscription_id, emit)
                for subscription_id, emit in list(self._subscribers.items())
            )
        )
        for subscription_id in failed:
            if subscription_id is not None:
                self._subscribers.pop(subscription_id, None)

    async def _set_phase(self, phase: ResearchPhase) -> None:
        self._phase = phase
        await self._publish("phase/change", {"phase": phase})

    def _workflow_active(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    def _successful_sota_id(self) -> str | None:
        experiment_id = self._tree.best_experiment_id()
        if experiment_id is None:
            return None
        experiment = self._tree.get_experiment(experiment_id)
        if (
            experiment.status is ExperimentStatus.SUCCEEDED
            and experiment.eval is not None
            and experiment.plan.kind in {"baseline", "search"}
        ):
            return experiment_id
        return None

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, object]:
        if method == ResearchMethod.PARSE_INTENT:
            return self._parse_intent(params)
        if method == ResearchMethod.TASK_CONFIGURE:
            return await self._configure(params)
        if method == ResearchMethod.SEARCH_START:
            return await self._search_start(params)
        if method == ResearchMethod.SEARCH_PAUSE:
            return await self._search_pause()
        if method == ResearchMethod.SEARCH_RESUME:
            return await self._search_resume()
        if method == ResearchMethod.SEARCH_STOP:
            return await self._search_stop()
        if method == ResearchMethod.VALIDATE_START:
            return await self._validate()
        if method == ResearchMethod.REPORT_GENERATE:
            return await self._report()
        if method == ResearchMethod.TREE_GET:
            return {"tree": self._tree.to_dict()}
        if method == ResearchMethod.TREE_SAVE:
            return await self._tree_save(params)
        if method == ResearchMethod.TREE_LOAD:
            return await self._tree_load(params)
        raise ValueError(f"unknown method: {method}")

    @staticmethod
    def _parse_intent(params: dict[str, Any]) -> dict[str, object]:
        message = str(params.get("message", "")).lower()
        task_type = "classification"
        if "regression" in message or "regress" in message:
            task_type = "regression"
        elif "cluster" in message:
            task_type = "clustering"
        data_type = "image" if "image" in message else "tabular"
        if "text" in message or "nlp" in message:
            data_type = "text"
        primary = "f1_macro"
        if "rmse" in message or "mae" in message:
            primary = "rmse"
        elif "accuracy" in message or "acc" in message:
            primary = "accuracy"
        elif "auc" in message or "roc" in message:
            primary = "roc_auc"
        return {
            "task_type": task_type,
            "data_type": data_type,
            "target_vars": [],
            "primary_metric": primary,
            "direction": (
                "minimize" if primary in {"rmse", "mae", "mse"} else "maximize"
            ),
            "needs_configuration": True,
        }

    async def _configure(self, params: dict[str, Any]) -> dict[str, object]:
        if self._workflow_active():
            raise RuntimeError("cannot configure while a research workflow is active")
        raw_metric = params.get("primary_metric", "f1_macro")
        metric = (
            raw_metric
            if isinstance(raw_metric, dict)
            else {
                "name": raw_metric,
                "direction": params.get("direction", "maximize"),
            }
        )
        task = TaskMetaData.model_validate(
            {
                "task_type": params.get("task_type", "classification"),
                "data_type": params.get("data_type", "tabular"),
                "target_vars": params.get("target_vars", []),
                "primary_metric": MetricSpec.model_validate(metric),
                "constraints": params.get("constraints", []),
            }
        )
        self._active_task = task
        await self._set_phase("CONFIGURED")
        return {"configured": True, "task": task.model_dump(mode="json")}

    async def _search_start(self, params: dict[str, Any]) -> dict[str, object]:
        if self._workflow_active():
            raise RuntimeError("research workflow is already active")
        if self._active_task is None:
            raise RuntimeError("SEARCH requires a configured task")
        if self._dependencies.search_factory is None:
            raise RuntimeError("search backend is not configured")
        has_baseline = self._successful_sota_id() is not None
        if not has_baseline and self._dependencies.prepare_baseline is None:
            raise RuntimeError("prepare backend is not configured")
        remaining = int(params.get("max_experiments", 10))
        max_no_improve = int(params.get("max_no_improve", 3))
        if remaining <= 0 or max_no_improve <= 0:
            raise ValueError("search budget values must be positive")
        self._budget = BudgetSnapshot(
            remaining=remaining, max_no_improve=max_no_improve
        )
        self._resume_gate.set()
        await self._set_phase("SEARCH" if has_baseline else "PREPARE")
        self._run_task = asyncio.create_task(
            self._run_search(has_baseline), name="research-search"
        )
        return {
            "phase": self._phase,
            "status": "started",
            "budget": self._budget.model_dump(mode="json"),
        }

    async def _run_search(self, has_baseline: bool) -> None:
        try:
            if not has_baseline:
                prepare = self._dependencies.prepare_baseline
                if prepare is None or self._active_task is None:
                    raise RuntimeError("prepare backend is not configured")
                await prepare(self._active_task, self._tree)
                await self._publish("tree/updated", {"tree": self._tree.to_dict()})
            await self._set_phase("SEARCH")
            factory = self._dependencies.search_factory
            if factory is None:
                raise RuntimeError("search backend is not configured")
            await factory(self._tree, self._budget, self._resume_gate.wait).run()
            await self._publish(
                "budget/update", {"budget": self._budget.model_dump(mode="json")}
            )
            await self._publish("tree/updated", {"tree": self._tree.to_dict()})
            await self._set_phase("COMPLETED")
        except asyncio.CancelledError:
            await self._set_phase("CANCELLED")
            raise
        except Exception:
            await self._set_phase("FAILED")
            raise
        finally:
            if self._run_task is asyncio.current_task():
                self._run_task = None

    async def _search_pause(self) -> dict[str, object]:
        if self._phase != "SEARCH" or not self._workflow_active():
            raise RuntimeError("pause requires an active SEARCH phase")
        self._resume_gate.clear()
        await self._set_phase("PAUSED")
        return {"phase": "PAUSED", "status": "paused"}

    async def _search_resume(self) -> dict[str, object]:
        if self._phase != "PAUSED" or not self._workflow_active():
            raise RuntimeError("resume requires a PAUSED search")
        self._resume_gate.set()
        await self._set_phase("SEARCH")
        return {"phase": "SEARCH", "status": "running"}

    async def _search_stop(self) -> dict[str, object]:
        task = self._run_task
        if task is None or task.done():
            raise RuntimeError("stop requires an active research workflow")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._run_task = None
        return {"phase": "CANCELLED", "status": "stopped"}

    async def _validate(self) -> dict[str, object]:
        if self._workflow_active():
            raise RuntimeError("cannot validate while a research workflow is active")
        if self._dependencies.validator is None:
            raise RuntimeError("validation backend is not configured")
        sota_id = self._successful_sota_id()
        if sota_id is None:
            raise RuntimeError("VALIDATE requires a successful SOTA")
        await self._set_phase("VALIDATE")
        try:
            result = await self._dependencies.validator.run(sota_id, self._tree)
        except Exception:
            await self._set_phase("FAILED")
            raise
        await self._publish("tree/updated", {"tree": self._tree.to_dict()})
        return result.model_dump(mode="json")

    async def _report(self) -> dict[str, object]:
        if self._workflow_active():
            raise RuntimeError("cannot report while a research workflow is active")
        if self._dependencies.reporter is None:
            raise RuntimeError("report backend is not configured")
        sota_id = self._successful_sota_id()
        if sota_id is None:
            raise RuntimeError("REPORT requires a successful SOTA")
        await self._set_phase("REPORT")
        try:
            report_ref = await self._dependencies.reporter.generate(sota_id, self._tree)
        except Exception:
            await self._set_phase("FAILED")
            raise
        await self._publish("tree/updated", {"tree": self._tree.to_dict()})
        await self._set_phase("COMPLETED")
        return {"report_ref": report_ref}

    async def _tree_save(self, params: dict[str, Any]) -> dict[str, object]:
        saved = self._tree.save(Path(str(params.get("path") or self._save_path)))
        return {"saved": True, "path": str(saved)}

    async def _tree_load(self, params: dict[str, Any]) -> dict[str, object]:
        if self._workflow_active():
            raise RuntimeError("cannot load a tree while a research workflow is active")
        self._tree = ResearchTree.load(Path(str(params.get("path") or self._save_path)))
        await self._publish("tree/updated", {"tree": self._tree.to_dict()})
        return {"loaded": True, "tree": self._tree.to_dict()}

    async def aclose(self) -> None:
        task = self._run_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._run_task = None
        self._subscribers.clear()
