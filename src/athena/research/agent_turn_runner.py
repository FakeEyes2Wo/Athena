"""Agent turn execution for the research runtime (Supervisor / Ideator / General).

拆自 ``ResearchRuntime``：把"运行一个 Agent turn 并解包结构化结果"的逻辑
集中到 ``AgentTurnRunner``。持有 ``runtime`` 引用访问组合根的共享基础设施。
"""

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athena.agents.general_agent import GeneralResult, register_general_agent
from athena.agents.ideator_agent import register_ideator_agent
from athena.agents.supervisor_agent import SUPERVISOR_AGENT_ID, SupervisorAnswer
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.research_models import Hypothesis, HypothesisBatch
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

    async def run_ideator_turn(self, count: int) -> list[Hypothesis]:
        """Run up to three Ideators against the EDA directory concurrently."""
        rt = self._runtime
        if rt._provider is None:
            raise RuntimeError("Ideator requires a registered Agent provider")
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
        eda_dir = str(eda_path)
        if not rt._registry.contains("ideator"):
            register_ideator_agent(
                rt._registry,
                provider=rt._provider,
                artifacts=rt._store,
                workspace=Path(eda_dir),
                runtime=rt._execution,
            )
        allocations = self._ideator_allocations(count)
        events = getattr(rt, "_events_bus", None)
        if events is not None:
            events.set_ideator_lanes(len(allocations))
            await events.publish_ideator_state()
        lane_results = await asyncio.gather(
            *(
                self._run_ideator_lane(f"ideator-{index}", target, Path(eda_dir))
                for index, target in enumerate(allocations, start=1)
            ),
            return_exceptions=True,
        )
        hypotheses: list[Hypothesis] = []
        failures: list[BaseException] = []
        for index, result in enumerate(lane_results, start=1):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failures.append(result)
                await rt.publish_output(
                    source="agent",
                    channel="error",
                    text=f"Ideator {index} failed: {result}",
                    plan=f"ideator-{index}",
                )
            else:
                hypotheses.extend(result)
        # 全部 lane 都失败时不再中断 SEARCH：每条失败已作为 error 输出发布。
        return hypotheses[:count]

    @staticmethod
    def _ideator_allocations(count: int) -> tuple[int, ...]:
        """Distribute one requested batch across at most three actual lanes."""
        if count <= 0:
            return ()
        worker_count = min(3, count)
        base, remainder = divmod(count, worker_count)
        return tuple(base + (index < remainder) for index in range(worker_count))

    async def _run_ideator_lane(
        self, label: str, target: int, eda_dir: Path
    ) -> list[Hypothesis]:
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
        return batch.hypotheses

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
