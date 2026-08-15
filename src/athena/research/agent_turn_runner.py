"""Agent turn execution for the research runtime (Supervisor / Ideator / General).

拆自 ``ResearchRuntime``：把"运行一个 Agent turn 并解包结构化结果"的逻辑
集中到 ``AgentTurnRunner``。持有 ``runtime`` 引用访问组合根的共享基础设施。
"""

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athena.agents.data_agent import DATA_AGENT_ID, register_data_agent
from athena.agents.general_agent import GeneralResult, register_general_agent
from athena.agents.ideator_agent import register_ideator_agent
from athena.agents.supervisor_agent import SUPERVISOR_AGENT_ID, SupervisorAnswer
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.research_models import EdaResult, Hypothesis, HypothesisBatch
from athena.core.tool import ToolRegistry
from athena.research.contracts import DataScriptBundle
from athena.research.supervisor.experiment import load_agent_result
from athena.research.supervisor.plans import wait_run_events

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime


async def _read_eval_handoff(
    store: ArtifactStore, evaluator_ref: ArtifactRef | None
) -> str:
    """Read the evaluator ``HANDOFF.md`` from a frozen bundle (empty when absent)."""
    if evaluator_ref is None:
        return ""
    try:
        bundle = DataScriptBundle.model_validate_json(
            await store.get_text(evaluator_ref)
        )
    except (ValueError, OSError):
        return ""
    if bundle.tree_ref is None:
        return ""
    try:
        tree = json.loads(await store.get_text(bundle.tree_ref))
    except (ValueError, OSError):
        return ""
    handoff_ref = tree.get("HANDOFF.md")
    if not isinstance(handoff_ref, str):
        return ""
    try:
        return await store.get_text(handoff_ref)
    except (ValueError, OSError):
        return ""


class AgentTurnRunner:
    """Run Supervisor / Ideator / General Agent turns via the runtime's infra."""

    def __init__(self, runtime: "ResearchRuntime") -> None:
        self._runtime = runtime
        self._ideator_round = 0

    async def run_supervisor_turn(self, text: str) -> str:
        rt = self._runtime
        if rt._provider is None:
            raise RuntimeError("SupervisorAgent provider is not registered")
        request = {"content": text, "context_refs": []}
        if rt._agents.has_agent(SUPERVISOR_AGENT_ID):
            run_id = await rt._agents.followup(SUPERVISOR_AGENT_ID, request)
        else:
            _agent_id, run_id = await rt._agents.create_root(
                "supervisor",
                request,
                agent_id=SUPERVISOR_AGENT_ID,
                name=SUPERVISOR_AGENT_ID,
            )
        summary = await rt._agents.wait_run(run_id)
        result = await load_agent_result(summary, rt._store, SupervisorAnswer)
        if result is None:
            raise RuntimeError(summary.error or "SupervisorAgent turn failed")
        await rt.publish_output(source="supervisor", channel="text", text=result.answer)
        return result.answer

    @staticmethod
    def _resolve_eda_dir(rt: "ResearchRuntime") -> str:
        """把 ``state.eda_dir`` 解析为本项目内的绝对 EDA 目录并做存在性校验。"""
        eda_dir = rt._state.eda_dir
        if not eda_dir:
            raise RuntimeError("EDA workspace not captured; PREPARE must run first")
        eda_path = Path(eda_dir)
        # 相对项目根的路径（新契约）解析为绝对；旧 state 遗留的绝对路径原样保留。
        if not eda_path.is_absolute():
            eda_path = (rt._root / eda_dir).resolve()
        root = getattr(rt, "_root", None)
        if not eda_path.is_dir() or (
            root is not None and not eda_path.is_relative_to(root)
        ):
            raise RuntimeError(
                "EDA workspace is stale or points outside this project "
                f"({eda_dir}); reset the project and re-run PREPARE"
            )
        return str(eda_path)

    async def run_ideator_turn(self, count: int) -> list[Hypothesis]:
        """Run configured Ideator lanes concurrently and return every hypothesis.

        ``count`` 是调度器填满空闲槽所需的最小数量；ideator 始终产出一整批
        （``ideator_count * hypotheses_per_ideator``），超出当前并发度的候选
        留在图中等待后续排序调度，因此生成阶段产出的所有假设都会进入 graph。

        任一 lane 通过 ``HypothesisBatch.eda_request`` 请求补充信息时，本回合会
        先派发 Data Agent 把补充 EDA 写回 EDA 目录，再返回假设；后续 Ideator
        回合会读取更新后的 EDA。
        """
        rt = self._runtime
        if rt._provider is None:
            raise RuntimeError("Ideator requires a registered Agent provider")
        eda_dir = self._resolve_eda_dir(rt)
        if not rt._registry.contains("ideator"):
            register_ideator_agent(
                rt._registry,
                provider=rt._provider,
                artifacts=rt._store,
                workspace=Path(eda_dir),
                runtime=rt._execution,
                extra_tools=rt.kaggle_tools("ideator"),
            )
        ideator_count = rt._state.ideator_count
        hypotheses_per_ideator = rt._state.hypotheses_per_ideator
        batch = max(count, ideator_count * hypotheses_per_ideator)
        allocations = self._ideator_allocations(batch, ideator_count)
        # 每轮用唯一前缀，避免跨轮复用 ``ideator-N`` 导致前端/ TUI 把新一轮
        # 追加到上一轮同 lane 的开放消息上。
        self._ideator_round += 1
        round_label = self._ideator_round
        events = getattr(rt, "_events_bus", None)
        if events is not None:
            events.set_ideator_lanes(len(allocations))
            await events.publish_ideator_state()
        lane_results = await asyncio.gather(
            *(
                self._run_ideator_lane(
                    f"ideator-{round_label}-{index}", target, Path(eda_dir)
                )
                for index, target in enumerate(allocations, start=1)
            ),
            return_exceptions=True,
        )
        hypotheses: list[Hypothesis] = []
        eda_requests: list[str] = []
        for index, result in enumerate(lane_results, start=1):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                await rt.publish_output(
                    source="agent",
                    channel="error",
                    text=f"Ideator {index} failed: {result}",
                    plan=f"ideator-{round_label}-{index}",
                )
            else:
                hypotheses.extend(result.hypotheses)
                request = (result.eda_request or "").strip()
                if request:
                    eda_requests.append(request)
        # 动态 EDA：任一 lane 请求补充信息时，派发 Data Agent 在 EDA 目录上做
        # 增量分析并写回 report/figures。合并多 lane 请求为一次分析任务。
        if eda_requests:
            await self.run_data_turn(
                "\n".join(f"- {request}" for request in eda_requests)
            )
        # 全部 lane 都失败时不再中断 SEARCH：每条失败已作为 error 输出发布。
        # 返回全部产物（不做截断）：所有生成假设都进入 graph，由调度器排序后按
        # 并发度逐个启动。
        return hypotheses

    async def run_data_turn(self, request: str) -> None:
        """派发 Data Agent 在 EDA 目录上做补充分析并写回 report/figures。"""
        rt = self._runtime
        if rt._provider is None:
            raise RuntimeError("Data Agent requires a registered Agent provider")
        eda_dir = self._resolve_eda_dir(rt)
        if not rt._registry.contains("data"):
            register_data_agent(
                rt._registry,
                provider=rt._provider,
                artifacts=rt._store,
                workspace=Path(eda_dir),
                runtime=rt._execution,
            )
        await rt.publish_output(
            source="agent",
            channel="text",
            text=f"补充 EDA 请求：{request}",
            plan=DATA_AGENT_ID,
        )
        task = {"content": request, "context_refs": []}
        if rt._agents.has_agent(DATA_AGENT_ID):
            run_id = await rt._agents.followup(DATA_AGENT_ID, task)
        else:
            _agent_id, run_id = await rt._agents.create_root(
                "data",
                task,
                agent_id=DATA_AGENT_ID,
                name=DATA_AGENT_ID,
            )
        summary = await wait_run_events(
            rt._agents,
            run_id,
            lambda kind, ref, data: rt._events_bus.project_agent_event(
                DATA_AGENT_ID, kind, ref, data
            ),
        )
        result = await load_agent_result(summary, rt._store, EdaResult)
        if result is None:
            raise RuntimeError(summary.error or "Data Agent turn failed")
        await rt.publish_output(
            source="agent",
            channel="text",
            text=f"补充 EDA 完成：{result.summary}",
            plan=DATA_AGENT_ID,
        )

    def _kaggle_tools(self) -> ToolRegistry | None:
        """General Agent 需要全量 Kaggle 工具。"""
        return self._runtime.kaggle_tools("general")

    @staticmethod
    def _ideator_allocations(count: int, lanes: int) -> tuple[int, ...]:
        """Distribute one requested batch across at most ``lanes`` ideator lanes."""
        if count <= 0 or lanes <= 0:
            return ()
        worker_count = min(lanes, count)
        base, remainder = divmod(count, worker_count)
        return tuple(base + (index < remainder) for index in range(worker_count))

    async def _run_ideator_lane(
        self, label: str, target: int, eda_dir: Path
    ) -> HypothesisBatch:
        """Run one independent Ideator and return its structured batch."""
        rt = self._runtime
        content = (
            f"Inspect the EDA workspace at {eda_dir} without modifying any "
            "files, then propose up to "
            f"{target} falsifiable hypotheses that could improve the primary "
            "metric. Return the hypotheses as structured output."
        )
        context_refs: list[ArtifactRef] = []
        handoff = await _read_eval_handoff(rt._store, rt._supervisor.evaluator_ref)
        if handoff:
            context_refs.append(
                await rt._store.put_text(
                    json.dumps({"eval_handoff": handoff}, ensure_ascii=False)
                )
            )
            content += (
                "\n\nThe evaluator contract (predictions directory layout and "
                "scoring criteria) is attached as context; read it before "
                "proposing hypotheses."
            )
        request = {"content": content, "context_refs": context_refs}
        _agent_id, run_id = await rt._agents.create_root("ideator", request, name=label)
        summary = await wait_run_events(
            rt._agents,
            run_id,
            lambda kind, ref, data: rt._events_bus.project_agent_event(
                label, kind, ref, data
            ),
        )
        batch = await load_agent_result(summary, rt._store, HypothesisBatch)
        if batch is None:
            raise RuntimeError(summary.error or "Ideator turn failed")
        return batch

    async def run_general_turn(self, task: str) -> dict[str, object]:
        """Dispatch one General Agent rooted at the project and return its result."""
        rt = self._runtime
        if rt._provider is None:
            raise RuntimeError("General Agent requires a registered Agent provider")
        if not rt._registry.contains("general"):
            register_general_agent(
                rt._registry,
                provider=rt._provider,
                artifacts=rt._store,
                project_root=rt._root,
                runtime=rt._execution,
                extra_tools=self._kaggle_tools(),
            )
        request = {"content": task, "context_refs": []}
        _agent_id, run_id = await rt._agents.create_root(
            "general", request, name="general"
        )
        summary = await wait_run_events(
            rt._agents,
            run_id,
            lambda kind, ref, data: rt._events_bus.project_agent_event(
                "general", kind, ref, data
            ),
        )
        result = await load_agent_result(summary, rt._store, GeneralResult)
        if result is None:
            raise RuntimeError(summary.error or "General Agent turn failed")
        return result.model_dump()
