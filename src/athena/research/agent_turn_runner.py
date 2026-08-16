"""Agent turn execution for the research runtime (Supervisor / Ideator / General).

拆自 ``ResearchRuntime``：把"运行一个 Agent turn 并解包结构化结果"的逻辑
集中到 ``AgentTurnRunner``。持有 ``runtime`` 引用访问组合根的共享基础设施。
"""

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from athena.agents.data_agent import DATA_AGENT_ID, register_data_agent
from athena.agents.general_agent import GeneralResult, register_general_agent
from athena.agents.ideator_agent import register_ideator_agent
from athena.agents.supervisor_agent import SUPERVISOR_AGENT_ID, SupervisorAnswer
from athena.core.contracts import ArtifactRef, ArtifactStore
from athena.core.research_models import EdaResult, Hypothesis, HypothesisBatch
from athena.core.tool import ToolRegistry
from athena.research.contracts import DataScriptBundle, GeneralTurnOutcome
from athena.research.idea_generation.gate import run_light_pipeline
from athena.research.idea_generation.idea_schemas import IdeatorHypothesisBatch
from athena.research.supervisor.experiment import load_agent_result
from athena.research.supervisor.plans import wait_run_events
from athena.retrieval.web_search import WebFetchTool, WebSearchTool, WebSession

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime


MAX_GATE_RETRIES = 2
"""门禁全拒后最多重新提案几次。

上限是硬的：门禁若持续拒绝，无限重生成就是死循环。取 2 是保守起点——一次让生成侧
按理由修正，一次留给它换个方向；没有经验依据，跑过几轮真实搜索后再校准。
"""

AGENT_TURN_TIMEOUT_SECONDS = 900
"""单个 Agent turn 的硬超时。

LLM/工具调用可能因上游无响应而永久挂起（无异常、无事件），前端看起来就是卡住。
给 wait 加超时，至少能把“挂起”变成可读的错误，而不是让 PREPARE 永远停在原地。
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
        self._ideator_round = 0

    async def run_supervisor_turn(self, text: str) -> str:
        """Run one serialized SupervisorAgent turn and return its human-facing answer."""
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
        try:
            summary = await asyncio.wait_for(
                rt._agents.wait_run(run_id), timeout=AGENT_TURN_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as error:
            raise RuntimeError(
                f"SupervisorAgent turn timed out after {AGENT_TURN_TIMEOUT_SECONDS}s"
            ) from error
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
            # PREPARE 失败后 state.eda_dir 可能为空，但默认 EDA 目录已建好；
            # 只要目录存在就继续，不因为状态字段缺失而误报“未捕获”。
            default_eda = getattr(rt, "_workspaces_root", None)
            if default_eda is not None and os.path.exists(Path(default_eda) / "eda"):
                eda_dir = str(Path(default_eda) / "eda")
            else:
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
        if getattr(rt, "_ideation", "ideageneration") == "debate":
            return await self._run_debate_ideator_turn(count)
        eda_dir = self._resolve_eda_dir(rt)
        if not rt._registry.contains("ideator"):
            register_ideator_agent(
                rt._registry,
                provider=rt._provider,
                artifacts=rt._store,
                workspace=Path(eda_dir),
                runtime=rt._execution,
                extra_tools=rt.ideator_tools(),
                gated=getattr(rt, "_ideation", "ideageneration") == "ideageneration",
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

    def _general_tools(self) -> ToolRegistry:
        """General Agent 的工具：Kaggle（若接入）+ 网页搜索。"""
        registry = ToolRegistry()
        kaggle = self._kaggle_tools()
        if kaggle is not None:
            for spec in kaggle.specs:
                registry.register(kaggle.resolve(spec.name))
        # web_search 与 web_fetch 共享同一会话，使搜索结果 ref_id 可被
        # web_fetch 直接打开/查找（对齐 Codex web.run 的 open/find）。
        web_session = WebSession()
        registry.register(WebSearchTool(session=web_session))
        registry.register(WebFetchTool(session=web_session))
        return registry

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
        corpus_ref = rt.survey_corpus_ref()
        if corpus_ref is not None:
            content += (
                f"\n\nA literature corpus is available for this task. "
                f"Pass corpus_ref={corpus_ref!r} to the paper_* tools to search and "
                "read it, and record the paper keys you actually used in each "
                "hypothesis's sources field."
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
        gated = getattr(rt, "_ideation", "ideageneration") == "ideageneration"
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
                        source="agent",
                        channel="error",
                        plan=label,
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

        ``ideageneration``：跑 Idea Generation 门禁（pre_gate + 视角审阅 +
        light_hard_gate），不合格的候选直接丢弃，不静默放行。
        ``baseline``：main 原有行为，产出即入库，作为消融对照组。
        """
        rt = self._runtime
        if getattr(rt, "_ideation", "ideageneration") != "ideageneration":
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
            batch.hypotheses,
            model=rt._model,
            artifacts=rt._store,
            progress=progress,
            rejections=rejections,
        )

    async def _run_debate_ideator_turn(self, count: int) -> list[Hypothesis]:
        """Run the debate-based Ideator (proposal -> review -> revision -> judge).

        This is the pre-migration ideator preserved behind ``--ideation debate``. It
        consumes the shared ResearchTree directly and returns hypotheses in judge order;
        there is no pipeline-local ranking either.
        """
        from athena.agents.ideator import Ideator  # 延迟导入避免循环依赖
        from athena.research.data_models import DataProfile  # 延迟导入避免循环依赖
        from athena.research.idea_generation.structured_chat import (  # 延迟导入避免循环依赖
            single_turn_structured_chat,
        )

        rt = self._runtime
        corpus_ref = rt.survey_corpus_ref()
        debate_tools = rt.ideator_tools()()

        class _StructuredResult:
            """Structured single-turn result adapter for the debate Ideator."""

            def __init__(self, value: object) -> None:
                self.output = value

        class _DebateAgentAdapter:
            """Adapt one structured chat call into the debate Ideator interface."""

            async def run(self, prompt, output_type=None, message_history=None):
                """Run one structured generation and return an Ideator-compatible result."""
                effective_prompt = prompt
                if corpus_ref is not None:
                    effective_prompt += (
                        f"\n\nA literature corpus is available for this task. "
                        f"Pass corpus_ref={corpus_ref!r} to the paper_* tools to search and "
                        "read it, and record the paper keys you actually used in each "
                        "hypothesis's sources field."
                    )
                value = await single_turn_structured_chat(
                    effective_prompt,
                    output_type,
                    model=rt._model,
                    artifacts=rt._store,
                    tools=debate_tools,
                )
                return _StructuredResult(value)

        def agent_factory(_role: str, _agent_index: int):
            """Return one debate agent adapter for a role lane."""
            return _DebateAgentAdapter()

        # 辩论 Ideator 的完整输入（DataProfile/papers/models）在 EDA-only 的 SEARCH
        # 组合根里没有现成来源；这里给最小画像 + 空文献/模型列表，保证模式可运行，
        # 后续接入 PREPARE 的 DataProfile 构建器后可替换为真实输入。
        profile = DataProfile(row_count=0, col_count=0, task_type_hint="eda_workspace")
        ideator = Ideator(agent_factory=agent_factory, artifacts=rt._store)
        try:
            result = await ideator.generate(profile, [], [], rt.tree)
        except Exception as error:  # noqa: BLE001 - debate 失败按 lane 失败上报
            await rt.publish_output(
                source="agent",
                channel="error",
                plan="ideator-debate",
                text=f"Debate Ideator failed: {error}",
            )
            return []
        return result.hypotheses[:count]

    async def run_general_turn(
        self, task: str, prior_agent_id: str | None = None
    ) -> GeneralTurnOutcome:
        """Dispatch one General Agent rooted at the project and return its outcome.

        ``prior_agent_id`` 续跑同一个 worker：进程重启后经其 rollout 恢复记忆，
        避免断点续传时重复调研。
        """
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
                extra_tools=self._general_tools(),
            )
        request = {"content": task, "context_refs": []}
        if prior_agent_id is not None and rt._agents.has_agent(prior_agent_id):
            agent_id = prior_agent_id
            run_id = await rt._agents.followup(agent_id, request)
        else:
            # ``agent_id=None`` 时新开随机 worker；给定 prior id 则经 rollout 恢复记忆。
            agent_id, run_id = await rt._agents.create_root(
                "general", request, agent_id=prior_agent_id, name="general"
            )
        if (
            rt._state.task_research_ref is None
            and rt._state.task_research_agent_id != agent_id
        ):
            # 断点续传：等待前先留下稳定 id，worker 超时/进程崩溃后仍能续跑同一线程。
            rt._state.task_research_agent_id = agent_id
            rt._state.save(rt._state_path)
        try:
            summary = await asyncio.wait_for(
                wait_run_events(
                    rt._agents,
                    run_id,
                    lambda kind, ref, data: rt._events_bus.project_agent_event(
                        "general", kind, ref, data
                    ),
                ),
                timeout=AGENT_TURN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as error:
            raise RuntimeError(
                f"General Agent turn timed out after {AGENT_TURN_TIMEOUT_SECONDS}s"
            ) from error
        result = await load_agent_result(summary, rt._store, GeneralResult)
        if result is None:
            raise RuntimeError(summary.error or "General Agent turn failed")
        return GeneralTurnOutcome(agent_id=agent_id, result=result.model_dump())
