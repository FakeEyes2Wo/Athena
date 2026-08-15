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
from athena.research.idea_generation.gate import run_light_pipeline
from athena.research.idea_generation.idea_schemas import IdeatorHypothesisBatch
from athena.research.supervisor.experiment import load_agent_result
from athena.research.supervisor.plans import wait_run_events

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime


MAX_GATE_RETRIES = 2
"""门禁全拒后最多重新提案几次。

上限是硬的：门禁若持续拒绝，无限重生成就是死循环。取 2 是保守起点——一次让生成侧
按理由修正，一次留给它换个方向；没有经验依据，跑过几轮真实搜索后再校准。
"""


def _regenerate_prompt(rejections: list[str], target: int) -> str:
    """把逐条拒绝理由拼成给同一个 Ideator 的重新提案请求。

    走 followup 而不是新建 agent：同一个 thread 保留了它原本的探索上下文，知道自己
    提过什么、为什么被拒，否则等于让一个全新的 agent 从零重猜。
    """
    reasons = "\n".join(f"- {reason}" for reason in rejections)
    return (
        "Every hypothesis you proposed was rejected by the quality gate:\n\n"
        f"{reasons}\n\n"
        f"Propose up to {target} different falsifiable hypotheses that address these "
        "specific objections. Do not restate a rejected hypothesis with reworded "
        "prose - change the substance, or explore a different mechanism entirely. "
        "Return the hypotheses as structured output."
    )


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
                gated=getattr(rt, "_ideation", "gated") == "gated",
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
        agent_id, run_id = await rt._agents.create_root("ideator", request, name=label)
        gated = getattr(rt, "_ideation", "gated") == "gated"
        schema = IdeatorHypothesisBatch if gated else HypothesisBatch

        # 门禁全拒时带理由重新提案：拒绝本身就是给生成侧的有效信号。上限是硬的——
        # 门禁若持续拒绝，无限重生成会变成死循环（真实跑测里 SEARCH 已因全拒而静默
        # 死过一次：返回空列表 -> generated=False -> run_search 直接 return -> 状态停在
        # RUNNING 既不推进也不终止）。
        for attempt in range(MAX_GATE_RETRIES + 1):
            summary = await wait_run_events(
                rt._agents,
                run_id,
                lambda kind, ref, data: rt._events_bus.project_agent_event(
                    label, kind, ref, data
                ),
            )
            batch = await load_agent_result(summary, rt._store, schema)
            if batch is None:
                raise RuntimeError(summary.error or "Ideator turn failed")

            rejections: list[str] = []
            kept = await self._finish_ideator_batch(batch, rejections=rejections)
            if kept or not rejections or attempt == MAX_GATE_RETRIES:
                if not kept and rejections:
                    await rt.publish_output(
                        source="agent", channel="error", plan=label,
                        text=(
                            f"gate rejected every candidate after "
                            f"{attempt + 1} attempt(s); this lane yields nothing"
                        ),
                    )
                return kept

            run_id = await rt._agents.followup(
                agent_id,
                {"content": _regenerate_prompt(rejections, target), "context_refs": []},
            )
        return []

    async def _finish_ideator_batch(
        self,
        batch: IdeatorHypothesisBatch | HypothesisBatch,
        *,
        rejections: list[str] | None = None,
    ) -> list[Hypothesis]:
        """按消融模式决定 Ideator 产出如何进入 ResearchTree。

        ``gated``：跑 Idea Generation 门禁（pre_gate + 视角审阅 + hard_gate + pairwise
        排序），不合格的候选直接丢弃，不静默放行。
        ``baseline``：main 原有行为，产出即入库，作为消融对照组。
        """
        rt = self._runtime
        if getattr(rt, "_ideation", "gated") != "gated":
            return list(batch.hypotheses)

        async def progress(message: str) -> None:  # noqa: D401
            """把门禁进度投影成普通输出事件。

            门禁全程只有 LLM 往返、没有本地计算，不报进度的话外部无法区分"正在跑十几个
            调用"和"卡死了"。
            """
            publish = getattr(rt, "publish_output", None)
            if publish is not None:
                await publish(source="agent", channel="text", text=f"gate> {message}")

        return await run_light_pipeline(
            batch.hypotheses, model=rt._model, artifacts=rt._store, progress=progress,
            rejections=rejections,
        )

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
