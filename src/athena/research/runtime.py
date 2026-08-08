"""ResearchRuntime — ProjectRuntime 的外部协议翻译层（dynamic-orchestration §8 条件 7）。

不再拥有第二套 Agent 生命周期：不创建 asyncio.Task、不保存 phase、不管理
pause、不运行 workflow coroutine。``dispatch`` 把 ResearchMethod 协议翻译为
``ProjectRuntime`` 的权威调用；phase/status 由项目状态投影提供。gui_gateway
等外部调用方继续通过 ``dispatch`` 使用。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from athena.core.research_tree import ResearchTree
from athena.research.budget import BudgetSnapshot
from athena.research.models import MetricSpec, TaskMetaData

if TYPE_CHECKING:
    from athena.research.project_runtime import ProjectRuntime


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
    """兼容占位（World A 后端已删除）；降级后委托 ProjectRuntime。"""

    prepare_baseline: PrepareBaselineFn | None = None
    search_factory: SearchFactory | None = None
    validator: Any | None = None
    reporter: Any | None = None


class ResearchRuntime:
    """ProjectRuntime 的外部协议翻译层（dynamic-orchestration §8 条件 7）。"""

    def __init__(
        self,
        *,
        tree: ResearchTree | None = None,
        save_path: str | Path = ".athena/research_tree.json",
        dependencies: ResearchWorkflowDependencies | None = None,
        project: "ProjectRuntime | None" = None,
    ) -> None:
        self._tree = tree or ResearchTree()
        self._save_path = Path(save_path)
        self._subscribers: dict[str, EmitFn] = {}
        self._dependencies = dependencies or ResearchWorkflowDependencies()
        self._project = project

    @property
    def phase(self) -> ResearchPhase:
        """外部投影词；委托 ProjectRuntime 投影，否则为 RUNNING。"""
        if self._project is not None:
            return self._project.projected_phase()
        return "IDLE"

    def project_status(self) -> str:
        """外部控制状态（§4.3）；委托 ProjectRuntime 投影，否则为 RUNNING。"""
        if self._project is not None:
            return self._project.project_status()
        return "RUNNING"

    @property
    def tree(self) -> ResearchTree:
        return self._tree

    @property
    def budget(self) -> BudgetSnapshot:
        if self._project is not None:
            return self._project.budget
        return BudgetSnapshot()

    def subscribe(self, emit: EmitFn) -> str:
        subscription_id = f"research:{uuid4().hex}"
        self._subscribers[subscription_id] = emit
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        self._subscribers.pop(subscription_id, None)

    async def _publish(self, kind: str, data: dict[str, object]) -> None:
        async def invoke(subscription_id: str, emit: EmitFn) -> None:
            try:
                result = emit(kind, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

        await asyncio.gather(
            *(invoke(sid, emit) for sid, emit in list(self._subscribers.items()))
        )

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, object]:
        if method == ResearchMethod.PARSE_INTENT:
            return self._parse_intent(params)
        if method not in ResearchMethod.ALL:
            raise ValueError(f"unknown method: {method}")
        if self._project is None:
            raise RuntimeError("ResearchRuntime requires a ProjectRuntime")
        if method == ResearchMethod.TASK_CONFIGURE:
            return await self._configure(params)
        if method == ResearchMethod.SEARCH_START:
            return await self._search_start(params)
        if method == ResearchMethod.SEARCH_PAUSE:
            self._project.pause()
            return {"phase": "PAUSED", "status": "paused"}
        if method == ResearchMethod.SEARCH_RESUME:
            self._project.resume()
            return {"phase": self._project.projected_phase(), "status": "running"}
        if method == ResearchMethod.SEARCH_STOP:
            await self._project.stop()
            return {"phase": "CANCELLED", "status": "stopped"}
        if method == ResearchMethod.VALIDATE_START:
            return await self._validate(params)
        if method == ResearchMethod.REPORT_GENERATE:
            return await self._report(params)
        if method == ResearchMethod.TREE_GET:
            return {"tree": self._project.tree.to_dict()}
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
        """把 TASK_CONFIGURE 参数翻译为 ProjectRuntime.configure。"""
        if self._project is None:
            raise RuntimeError("ResearchRuntime requires a ProjectRuntime")
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
        await self._project.configure(task)
        return {"configured": True, "task": task.model_dump(mode="json")}

    async def _search_start(self, params: dict[str, Any]) -> dict[str, object]:
        """把 SEARCH_START 翻译为 ProjectRuntime.run_search（单假设确定性步骤）。"""
        if self._project is None:
            raise RuntimeError("ResearchRuntime requires a ProjectRuntime")
        hypothesis = str(params.get("hypothesis", "default hypothesis"))
        ref = await self._project.run_search(hypothesis)
        return {
            "status": "started",
            "phase": self._project.projected_phase(),
            "sota_ref": ref,
        }

    async def _validate(self, params: dict[str, Any]) -> dict[str, object]:
        """把 VALIDATE_START 翻译为 ProjectRuntime.run_validate。"""
        if self._project is None:
            raise RuntimeError("ResearchRuntime requires a ProjectRuntime")
        ref = str(params.get("experiment_ref", "sota"))
        validation_ref = await self._project.run_validate(ref)
        return {"validation_ref": validation_ref}

    async def _report(self, params: dict[str, Any]) -> dict[str, object]:
        """把 REPORT_GENERATE 翻译为 ProjectRuntime.run_report。"""
        if self._project is None:
            raise RuntimeError("ResearchRuntime requires a ProjectRuntime")
        report_text = str(params.get("report_text", "final report"))
        report_ref = await self._project.run_report(report_text)
        return {"report_ref": report_ref}

    async def _tree_save(self, params: dict[str, Any]) -> dict[str, object]:
        saved = self._tree.save(Path(str(params.get("path") or self._save_path)))
        return {"saved": True, "path": str(saved)}

    async def _tree_load(self, params: dict[str, Any]) -> dict[str, object]:
        if self._project is None:
            raise RuntimeError("ResearchRuntime requires a ProjectRuntime")
        self._tree = (
            ResearchTree.from_dict(self._project.tree.to_dict())
            if self._project.tree is not None
            else ResearchTree()
        )
        await self._publish("tree/updated", {"tree": self._tree.to_dict()})
        return {"loaded": True, "tree": self._tree.to_dict()}

    async def aclose(self) -> None:
        self._subscribers.clear()
