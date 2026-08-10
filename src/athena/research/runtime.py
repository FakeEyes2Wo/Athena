"""ResearchRuntime — Supervisor 子系统的公开门面与项目 composition root（supervisor_design §2.2）。

App Server / GUI Gateway 只通过 ``dispatch`` 使用；ResearchRuntime 负责公开调用面、
项目依赖装配、execution 生命周期、恢复、状态查询和事件发布。具体规划、校验、
执行与持久化由 supervisor 子系统的独立组件承担：
Coordinator / Planner / Validator / Executor / Journal / StateStore / AgentRuntime。
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.builtin_agents import CodeAgent, IdeatorAgent, PlotAgent
from athena.agents.data_agent import DataAgent
from athena.agents.init_agent import InitAgent
from athena.agents.reflection_agent import (
    ReflectionAgent,
    build_reflection_run_impl,
)
from athena.research.data_service import DatasetService
from athena.research.evaluation import TrustedEvaluator
from athena.research.script_runner import DataScriptRunner
from athena.research.search import SearchService
from athena.research.services import ResearchServices
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.registry import AgentFactory, AgentTypeRegistry
from athena.core.agent.types import AgentSpec, AgentStatus, JsonCodec
from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import VersionedBundle
from athena.core.contracts import new_id
from athena.core.research_tree import ResearchTree
from athena.research.contracts import ExecutionConfig
from athena.research.models import MetricSpec, TaskMetaData
from athena.research.supervisor.coordinator import SupervisorCoordinator
from athena.research.supervisor.executor import PlanExecutor
from athena.research.supervisor.journal import PlanJournal
from athena.research.supervisor.models import ControlStatus, utc_now
from athena.research.supervisor.planner import DeterministicSupervisorPlanner
from athena.research.supervisor.state import ProjectStateStore, project_phase
from athena.research.supervisor.validator import PlanValidator


class ResearchMethod:
    """外部控制面方法集（supervisor_design §3，不含阶段推进命令）。"""

    PARSE_INTENT = "PARSE_INTENT"
    TASK_CONFIGURE = "TASK_CONFIGURE"
    RUN = "RUN"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP = "STOP"
    STATUS = "STATUS"
    REQUESTS_GET = "REQUESTS_GET"
    HUMAN_REPLY = "HUMAN_REPLY"
    TREE_GET = "TREE_GET"
    TREE_SAVE = "TREE_SAVE"
    TREE_LOAD = "TREE_LOAD"
    ALL = frozenset(
        {
            PARSE_INTENT,
            TASK_CONFIGURE,
            RUN,
            PAUSE,
            RESUME,
            STOP,
            STATUS,
            REQUESTS_GET,
            HUMAN_REPLY,
            TREE_GET,
            TREE_SAVE,
            TREE_LOAD,
        }
    )


EmitFn = Callable[[str, dict[str, object]], Awaitable[None] | None]


class ResearchRuntime:
    """Supervisor 子系统的公开门面与项目 composition root。

    构造时装配共享基础设施（ArtifactStore / Bundle / Registry / AgentRuntime /
    Supervisor Journal / StateStore / Planner / Validator / Executor / Coordinator），
    打开项目的 ``.athena/supervisor.db``。``dispatch`` 是外部唯一调用面。
    """

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        model: str | None = None,
        client: Any = None,
        inner_builder: Callable[..., Any] | None = None,
    ) -> None:
        """构造 composition root；给定 ``model`` 时自动注册静态 worker。

        ``model`` 必填才注册 LLM worker（无回退）；CLI 传入 ``settings.model_name()``。
        """
        self._root = Path(project_root or ".")
        self._subscribers: dict[str, EmitFn] = {}
        self._coordinator_task: asyncio.Task | None = None

        self._store = LocalArtifactStore(self._root / "artifacts")
        self._bundle = VersionedBundle(self._store)
        self._registry = AgentTypeRegistry()
        self._runtime = AgentRuntime(
            type_registry=self._registry,
            project_root=self._root,
        )
        self._journal = PlanJournal(self._root / ".athena" / "supervisor.db")
        self._state = ProjectStateStore(self._journal)
        self._tree = ResearchTree()
        self._planner = DeterministicSupervisorPlanner(self._journal, self._state)
        self._validator = PlanValidator(self._journal, self._state)
        # 确定性领域服务注册（RUN_SERVICE）：DatasetService / DataScriptRunner
        # 由 composition root 直接装配，不引入 service-manager 框架。
        self._script_runner = DataScriptRunner(
            store=self._store, workdir=self._root / ".athena" / "runs"
        )
        self._services = ResearchServices(
            store=self._store,
            dataset=DatasetService(workdir=self._root / ".athena" / "data"),
            runner=self._script_runner,
            search=SearchService(self._tree),
            evaluator=TrustedEvaluator(self._script_runner),
        )
        self._executor = PlanExecutor(
            journal=self._journal,
            state=self._state,
            runtime=self._runtime,
            store=self._store,
            services=self._services,
        )
        self._coordinator = SupervisorCoordinator(
            journal=self._journal,
            state=self._state,
            planner=self._planner,
            validator=self._validator,
            executor=self._executor,
        )
        if model is not None:
            self.register_defaults(
                model=model, client=client, inner_builder=inner_builder
            )

    # ---- 基础设施访问（composition root 暴露给内部与测试） ----

    @property
    def kernel(self) -> AgentRuntime:
        """AgentRuntime 门面（worker 生命周期）。"""
        return self._runtime

    @property
    def store(self) -> LocalArtifactStore:
        """项目内容寻址 ArtifactStore。"""
        return self._store

    @property
    def bundle(self) -> VersionedBundle:
        """DataAnalysis 版本所有权链。"""
        return self._bundle

    @property
    def tree(self) -> ResearchTree:
        """ResearchTree（假设/实验/SOTA 关系唯一所有者）。"""
        return self._tree

    @property
    def journal(self) -> PlanJournal:
        return self._journal

    @property
    def state(self) -> ProjectStateStore:
        return self._state

    @property
    def coordinator(self) -> SupervisorCoordinator:
        return self._coordinator

    @property
    def phase(self) -> str:
        """外部投影词（supervisor_design §4.1，由已提交事实派生）。"""
        return project_phase(self._state.facts()).value

    def project_status(self) -> str:
        """外部控制状态（supervisor_design §4.2）。"""
        return self._state.control_status().value

    @property
    def registered_worker_types(self) -> tuple[str, ...]:
        """已注册 worker 类型（supervisor 不再注册；供测试与 RUN 断言）。"""
        return self._registry.types

    # ---- worker 注册（composition root） ----

    def register_defaults(
        self,
        *,
        model: str | None = None,
        client: Any = None,
        inner_builder: Callable[..., Any] | None = None,
    ) -> None:
        """注册静态 worker 类型；model 必填（LLM 驱动，无回退）。

        supervisor 不再作为 registry 类型（supervisor_design §2.1）：Supervisor 是
        确定性协调子系统，由本门面直接持有，不经 AgentRuntime 派发。
        # TODO(data-runtime): 出现已批准的非 Python 数据脚本需求后，新增对应 runtime adapter；首版仅支持 python-uv。
        """
        if model is None:
            raise RuntimeError(
                "register_defaults requires a model: set MODEL_NAME + "
                "DEEPSEEK_API_KEY/BASE_URL in .env and pass model=..."
            )
        builders: dict[str, Callable[[str], BaseAgentRunner]] = {
            "init": lambda _aid: BaseAgentRunner(
                InitAgent(
                    self._store,
                    model=model,
                    client=client,
                    inner_builder=inner_builder,
                )
            ),
            "data": lambda aid: BaseAgentRunner(
                DataAgent(
                    self._store,
                    self._bundle,
                    aid,
                    model=model,
                    client=client,
                    inner_builder=inner_builder,
                )
            ),
            "plot": lambda _aid: BaseAgentRunner(PlotAgent(self._store)),
            "reflection": lambda _aid: BaseAgentRunner(
                ReflectionAgent(
                    self._store,
                    run_impl=build_reflection_run_impl(
                        self._store, model=model, client=client
                    ),
                )
            ),
            "ideator": lambda _aid: BaseAgentRunner(IdeatorAgent(self._store)),
            "code": lambda _aid: BaseAgentRunner(CodeAgent(self._store)),
        }
        for name, build in builders.items():
            self._registry.register(name, self._worker_factory(build))

    @staticmethod
    def _worker_factory(builder: Callable[[str], BaseAgentRunner]) -> AgentFactory:
        """构造 registry factory：按 agent_id 创建静态 worker 的 AgentSpec（§2.2 所有权边界）。"""
        return lambda agent_id, _config=None: AgentSpec(
            runner=builder(agent_id), codec=JsonCodec()
        )

    # ---- 事件订阅 ----

    def subscribe(self, emit: EmitFn) -> str:
        subscription_id = new_id("research")
        self._subscribers[subscription_id] = emit
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        self._subscribers.pop(subscription_id, None)

    async def _publish(self, kind: str, data: dict[str, object]) -> None:
        async def invoke(sid: str, emit: EmitFn) -> None:
            try:
                result = emit(kind, data)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise  # 取消应传播，不是订阅者失败
            except Exception:
                logger.exception("subscriber %s failed handling event %s", sid, kind)

        await asyncio.gather(
            *(invoke(sid, emit) for sid, emit in list(self._subscribers.items()))
        )

    # ---- 公开调用面 ----

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, object]:
        """外部控制面唯一入口；返回稳定 JSON 快照（§3/§8）。"""
        if method == ResearchMethod.PARSE_INTENT:
            return self._parse_intent(params)
        if method not in ResearchMethod.ALL:
            raise ValueError(f"unknown method: {method}")
        handler = {
            ResearchMethod.TASK_CONFIGURE: self._task_configure,
            ResearchMethod.RUN: self._run,
            ResearchMethod.PAUSE: self._pause,
            ResearchMethod.RESUME: self._resume,
            ResearchMethod.STOP: self._stop,
            ResearchMethod.STATUS: self._status,
            ResearchMethod.REQUESTS_GET: self._requests_get,
            ResearchMethod.HUMAN_REPLY: self._human_reply,
            ResearchMethod.TREE_GET: self._tree_get,
            ResearchMethod.TREE_SAVE: self._tree_save,
            ResearchMethod.TREE_LOAD: self._tree_load,
        }[method]
        return await handler(params)

    @staticmethod
    def _parse_intent(params: dict[str, Any]) -> dict[str, object]:
        """PARSE_INTENT：原样返回意图文本；不做确定性任务/指标推断（LLM 承担）。"""
        message = str(params.get("message", ""))
        return {"intent": message, "needs_configuration": True}

    async def _task_configure(self, params: dict[str, Any]) -> dict[str, object]:
        """TASK_CONFIGURE：保存 task/config 与源路径候选；不硬编码 task/data/metric。

        给定 ``task_type`` 时冻结结构化任务（此时 data_type/primary_metric 必填）；
        否则只保存自然语言 ``task`` intent。execution 首次访问时显式创建。
        """
        data_path = str(params.get("data_path", ""))
        # 单一、明确的数据路径：把用户提供的相对路径解析为进程 CWD 下的绝对路径
        # （真实入口 ``uv run src/main.py`` 的 ``examples/titanic`` 相对仓库根），
        # ingest 与全部 worker 收到同一绝对路径——worker 在独立临时 workspace 中
        # 也能 ``ls`` 读取，不再依赖与 ingest 相同的 CWD。
        if data_path:
            data_path = str(Path(data_path).expanduser().resolve())
        target_vars = params.get("target_vars", [])
        target = str(params.get("target") or (target_vars[0] if target_vars else ""))
        config = ExecutionConfig(
            interaction_mode=params.get("interaction_mode", "interactive"),
            kfold_policy=params.get("kfold_policy", "auto"),
        )
        if params.get("task_type") is not None:
            data_type = params.get("data_type")
            metric = params.get("primary_metric")
            if data_type is None or metric is None:
                raise ValueError(
                    "typed task config requires 'data_type' and 'primary_metric'"
                )
            task = TaskMetaData.model_validate(
                {
                    "task_type": str(params["task_type"]),
                    "data_type": str(data_type),
                    "target_vars": list(target_vars),
                    "primary_metric": MetricSpec.model_validate(
                        metric
                        if isinstance(metric, dict)
                        else {
                            "name": metric,
                            "direction": params.get("direction", "maximize"),
                        }
                    ),
                    "constraints": params.get("constraints", []),
                }
            )
            task_text = task.model_dump_json()
        else:
            task_text = json.dumps(
                {"intent": str(params.get("task", "")), "data_path": data_path},
                ensure_ascii=False,
            )
        task_ref = await self._store.put_text(task_text)
        execution_config_ref = await self._store.put_text(config.model_dump_json())
        execution_id = self._journal.active_execution_id()
        if execution_id is None:
            execution_id = self._journal.create_execution(
                interaction_mode=config.interaction_mode
            )["execution_id"]
        self._coordinator.bind(execution_id, on_event=self._forward_event)
        self._state.commit_facts(
            {
                "task_ref": task_ref,
                "execution_config_ref": execution_config_ref,
                # Planner 是确定性无 Artifact 读取组件，因此把配置的关键限额
                # 投影为事实供其读取（修订预算、每轮选中候选等）。
                "prepare_revision_limit": config.max_prepare_revisions,
                "selected_hypotheses_per_round": config.selected_hypotheses_per_round,
                "task_config": {
                    "init_payload": {"data_path": data_path, "target": target},
                    "data_payload": {"data_path": data_path, "target": target},
                    "ideator_payload": {"data_path": data_path, "target": target},
                    "code_payload": {"data_path": data_path, "target": target},
                },
            }
        )
        return {
            "configured": True,
            "interaction_mode": config.interaction_mode,
            "phase": self.phase,
        }

    async def _run(self, params: dict[str, Any]) -> dict[str, object]:
        """RUN：创建或复用活动 execution，启动后台 Coordinator 后立即返回。

        CANCELLED 后的再次 RUN 创建新 execution（旧 execution 保持 terminal）。
        """
        del params
        config = await self._execution_config()
        mode = config.interaction_mode if config else "interactive"
        execution_id = self._journal.active_execution_id()
        if execution_id is None:  # 无活动 execution（含仅 CANCELLED terminal）→ 新建
            execution_id = self._journal.create_execution(interaction_mode=mode)[
                "execution_id"
            ]
            self._coordinator.bind(execution_id, on_event=self._forward_event)
        self._state.set_control_status(ControlStatus.RUNNING)
        self._start_coordinator()
        return {
            "execution_id": execution_id,
            "status": "running",
            "phase": self.phase,
            "interaction_mode": mode,
        }

    async def _pause(self, params: dict[str, Any]) -> dict[str, object]:
        del params
        self._state.set_control_status(ControlStatus.PAUSED)
        return {"phase": self.phase, "status": "paused"}

    async def _resume(self, params: dict[str, Any]) -> dict[str, object]:
        del params
        self._state.set_control_status(ControlStatus.RUNNING)
        self._start_coordinator()
        return {"phase": self.phase, "status": "running"}

    async def _stop(self, params: dict[str, Any]) -> dict[str, object]:
        del params
        self._state.set_control_status(ControlStatus.CANCELLED)
        # 中断活动 worker，避免后台继续运行
        for snapshot in self._runtime.list_agents():
            if snapshot.status is AgentStatus.RUNNING:
                await self._runtime.interrupt(snapshot.agent_id, "supervisor stop")
        return {"phase": self.phase, "status": "stopped"}

    async def _status(self, params: dict[str, Any]) -> dict[str, object]:
        """STATUS：不隐式创建 execution；可用 ``execution_id`` 显式选择。

        未指定时默认活动 execution，否则最近 terminal；全无 →
        ``execution=None, phase=IDLE``（supervisor_design §8）。
        """
        execution_id = params.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            execution_id = self._journal.active_execution_id()
            if execution_id is None:
                execution_id = self._journal.latest_execution_id()
        if execution_id is None:
            return self._idle_status()
        facts = self._state.facts()
        budget = self._state.budget()
        control = self._state.control_status()
        open_requests = self._journal.open_human_requests(execution_id)
        return {
            "execution": {
                "id": execution_id,
                "status": control.value,
                "phase": self.phase,
            },
            "state_version": self._journal.snapshot_version(),
            "sota": {"test_score": None, "target_test_score": None},
            "progress": {
                "search_experiments": {
                    "used": budget.search_experiments_used,
                    "limit": budget.max_search_experiments,
                },
                "consecutive_no_improvement": {
                    "used": budget.consecutive_no_improvement,
                    "limit": budget.max_consecutive_no_improvement,
                },
            },
            "budgets": {"plans_remaining": budget.plans_remaining},
            "human_request": (
                {
                    "request_id": open_requests[0].request_id,
                    "kind": open_requests[0].reason_code,
                    "question": open_requests[0].question,
                    "options": open_requests[0].options,
                    "created_at": open_requests[0].created_at,
                }
                if open_requests
                else None
            ),
            "refs": {
                "task": facts.task_ref,
                "eval_spec": facts.eval_spec_ref,
                "graph": None,
            },
            "error": None,
            "updated_at": utc_now(),
        }

    def _idle_status(self) -> dict[str, object]:
        """无 execution 时的 STATUS 快照（execution=None, phase=IDLE）。"""
        return {
            "execution": None,
            "phase": "IDLE",
            "state_version": self._journal.snapshot_version(),
            "sota": {"test_score": None, "target_test_score": None},
            "progress": {
                "search_experiments": {"used": 0, "limit": 20},
                "consecutive_no_improvement": {"used": 0, "limit": 5},
            },
            "budgets": {"plans_remaining": 100},
            "human_request": None,
            "refs": {"task": None, "eval_spec": None, "graph": None},
            "error": None,
            "updated_at": utc_now(),
        }

    async def _requests_get(self, params: dict[str, Any]) -> dict[str, object]:
        del params
        requests = self._journal.open_human_requests(self._execution_id())
        return {
            "requests": [
                {
                    "request_id": r.request_id,
                    "execution_id": r.execution_id,
                    "reason_code": r.reason_code,
                    "question": r.question,
                    "context_refs": r.context_refs,
                    "input_mode": r.input_mode,
                    "options": r.options,
                    "allow_free_text": r.allow_free_text,
                }
                for r in requests
            ]
        }

    async def _human_reply(self, params: dict[str, Any]) -> dict[str, object]:
        request_id = str(params["request_id"])
        answer = str(params["answer"])
        self._journal.answer_human_request(request_id, answer, "human")
        self._start_coordinator()
        return {"answered": True, "request_id": request_id}

    async def _tree_get(self, params: dict[str, Any]) -> dict[str, object]:
        del params
        return {"tree": self._tree.to_dict()}

    async def _tree_save(self, params: dict[str, Any]) -> dict[str, object]:
        saved = self._tree.save(
            Path(
                str(params.get("path") or self._root / ".athena" / "research_tree.json")
            )
        )
        return {"saved": True, "path": str(saved)}

    async def _tree_load(self, params: dict[str, Any]) -> dict[str, object]:
        path = Path(str(params["path"]))
        self._tree = ResearchTree.load(path)
        await self._publish("tree/updated", {"tree": self._tree.to_dict()})
        return {"loaded": True, "tree": self._tree.to_dict()}

    # ---- 内部 ----

    def _start_coordinator(self) -> None:
        """若未在运行则启动后台 Coordinator（RUN/RESUME/HUMAN_REPLY 共用）。"""
        if self._coordinator.execution_id is None:
            return  # 未绑定 execution → 无协调循环
        if self._coordinator_task is None or self._coordinator_task.done():
            self._coordinator_task = asyncio.create_task(self._coordinator.run())

    async def _forward_event(self, kind: str, data: dict[str, object]) -> None:
        """把 Coordinator 的 durable event 转发给订阅者。"""
        await self._publish(kind, data)

    async def _execution_config(self) -> ExecutionConfig | None:
        """读取持久化的 ExecutionConfig；未配置返回 None。"""
        ref = self._state.facts().execution_config_ref
        if ref is None:
            return None
        return ExecutionConfig.model_validate_json(await self._store.get_text(ref))

    async def aclose(self) -> None:
        self._subscribers.clear()
        if self._coordinator_task is not None and not self._coordinator_task.done():
            self._coordinator_task.cancel()
            try:
                await self._coordinator_task
            except asyncio.CancelledError:
                pass  # 预期的协调器取消
            except Exception:
                logger.exception("coordinator task raised during shutdown")
        await self._runtime.aclose()
        self._journal.close()
