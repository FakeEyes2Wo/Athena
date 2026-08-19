"""Agent turn execution for the research runtime (Supervisor / Ideator / General).

拆自 ``ResearchRuntime``：把"运行一个 Agent turn 并解包结构化结果"的逻辑
集中到 ``AgentTurnRunner``。持有 ``runtime`` 引用访问组合根的共享基础设施。
"""

import asyncio
import itertools
import json
import os
import time
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from athena.agents.data_agent import DATA_AGENT_ID, register_data_agent
from athena.agents.general_agent import GeneralResult, register_general_agent
from athena.agents.ideator_agent import (
    SEARCH_IDEATOR_PROFILES,
    IdeatorProfile,
    register_ideator_agent,
)
from athena.agents.kaggle_handoff_agent import (
    KAGGLE_HANDOFF_AGENT_ID,
    KAGGLE_HANDOFF_AGENT_TYPE,
    KAGGLE_HANDOFF_FILENAME,
    KaggleHandoffResult,
    register_kaggle_handoff_agent,
)
from athena.agents.supervisor_agent import SUPERVISOR_AGENT_ID, SupervisorAnswer
from athena.core.contracts import ArtifactRef
from athena.core.research_models import EdaResult, Hypothesis, HypothesisBatch
from athena.core.tool import ToolRegistry
from athena.kaggle.wiring import kaggle_slug_from_task
from athena.research.contracts import GeneralTurnOutcome
from athena.research.idea_generation.citation_support import (
    SUPPORT_PROMPT,
    SupportVerdict,
    format_evidence,
    parse_verdict,
)
from athena.research.idea_generation.gate import run_light_pipeline
from athena.research.idea_generation.idea_schemas import IdeatorHypothesisBatch
from athena.research.supervisor.experiment import (
    handoff_block,
    load_agent_result,
    read_eval_handoff,
)
from athena.research.supervisor.plans import wait_run_events
from athena.retrieval.web_search import WebFetchTool, WebSearchTool, WebSession
from athena.utils.single_turn_chat import single_turn_chat

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime

logger = logging.getLogger(__name__)


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

TURN_HEARTBEAT_SECONDS = 300
"""等待 Agent turn 期间向 UI 报告“仍在运行”的间隔。"""


async def _wait_run_with_heartbeat(
    rt,
    agents,
    run_id,
    *,
    agent_id: str,
    label: str,
    plan: str | None = None,
    project: bool = True,
):
    """等待 Agent run：每 5 分钟发布心跳，15 分钟硬超时并 interrupt。"""
    deadline = time.monotonic() + AGENT_TURN_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _interrupt_agent(agents, agent_id, f"{plan or agent_id}_turn_timeout")
            raise RuntimeError(f"{label} timed out after {AGENT_TURN_TIMEOUT_SECONDS}s")
        if project:
            events_bus = getattr(rt, "_events_bus", None)
            if events_bus is not None:
                publish = lambda kind, ref, data: events_bus.project_agent_event(  # noqa: E731
                    plan or agent_id, kind, ref, data
                )
            else:
                publish = None
            waiter = wait_run_events(agents, run_id, publish)
        else:
            waiter = agents.wait_run(run_id)
        try:
            return await asyncio.wait_for(
                waiter, timeout=min(TURN_HEARTBEAT_SECONDS, remaining)
            )
        except asyncio.TimeoutError:
            publish_output = getattr(rt, "publish_output", None)
            if publish_output is not None:
                await publish_output(
                    source="agent",
                    channel="text",
                    text=f"{label} still working (heartbeat)…",
                    plan=plan or agent_id,
                )


MAX_KAGGLE_HANDOFF_CHARS = 12_000
"""注入 Ideator 的 Kaggle handoff 文本上限，防止把超大内容塞进每轮 prompt。"""


async def _interrupt_agent(agents, agent_id: str, reason: str) -> None:
    """Best-effort 打断 worker；worker 已结束或不存在时忽略。"""
    try:
        await agents.interrupt(agent_id, reason)
    except Exception:
        pass


class _DebateProfile(BaseModel):
    """辩论 Ideator 的最小数据画像（EDA-only SEARCH 没有完整 DataProfile 来源）。"""

    row_count: int = 0
    col_count: int = 0
    task_type_hint: str = "eda_workspace"


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
        summary = await _wait_run_with_heartbeat(
            rt,
            rt._agents,
            run_id,
            agent_id=SUPERVISOR_AGENT_ID,
            label="SupervisorAgent turn",
            plan=SUPERVISOR_AGENT_ID,
            project=False,
        )
        result = await load_agent_result(summary, rt._store, SupervisorAnswer)
        if result is None:
            raise RuntimeError(summary.error or "SupervisorAgent turn failed")
        await rt.publish_output(source="supervisor", channel="text", text=result.answer)
        return result.answer

    @staticmethod
    def _resolve_eda_dir(rt: "ResearchRuntime") -> str:
        """把 ``state.eda_dir`` 解析为本项目内的绝对 EDA 目录并做存在性校验。"""
        eda_dir = rt.state.eda_dir
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
        # 按 state.handoff_sources 收集启用的 handoff；失败来源只返回空文本，
        # 不影响本地 EDA-only 的 idea generation。
        handoff_texts = await self._collect_handoff_texts()
        if getattr(rt, "_ideation", "ideageneration") == "debate":
            return await self._run_debate_ideator_turn(count, handoff_texts)
        eda_dir = self._resolve_eda_dir(rt)
        ideation = getattr(rt, "_ideation", "ideageneration")
        if ideation == "ideageneration":
            for profile in SEARCH_IDEATOR_PROFILES:
                if not rt._registry.contains(profile.agent_type):
                    register_ideator_agent(
                        rt._registry,
                        provider=rt._provider,
                        artifacts=rt._store,
                        workspace=Path(eda_dir),
                        runtime=rt._execution,
                        extra_tools=rt.ideator_tools(),
                        gated=True,
                        profile=profile,
                    )
            lane_profiles = itertools.cycle(SEARCH_IDEATOR_PROFILES)
        else:
            if not rt._registry.contains("ideator"):
                register_ideator_agent(
                    rt._registry,
                    provider=rt._provider,
                    artifacts=rt._store,
                    workspace=Path(eda_dir),
                    runtime=rt._execution,
                    extra_tools=rt.ideator_tools(),
                    gated=False,
                )
            lane_profiles = itertools.repeat(None)
        ideator_count = rt.state.ideator_count
        hypotheses_per_ideator = rt.state.hypotheses_per_ideator
        batch = max(count, ideator_count * hypotheses_per_ideator)
        allocations = self._ideator_allocations(batch, ideator_count)
        # 每轮用唯一前缀，避免跨轮复用 ``ideator-N`` 导致前端/ TUI 把新一轮
        # 追加到上一轮同 lane 的开放消息上。
        self._ideator_round += 1
        round_label = self._ideator_round
        # 引用核验按"本轮谁打开过哪几篇"判定，因此每轮先把上一轮的会话账本丢掉。
        rt.start_corpus_round()
        events = getattr(rt, "_events_bus", None)
        if events is not None:
            events.set_ideator_lanes(len(allocations))
            await events.publish_ideator_state()
        lane_results = await asyncio.gather(
            *(
                self._run_ideator_lane(
                    f"ideator-{round_label}-{index}",
                    target,
                    Path(eda_dir),
                    **self._lane_kwargs(handoff_texts, next(lane_profiles)),
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
        summary = await _wait_run_with_heartbeat(
            rt,
            rt._agents,
            run_id,
            agent_id=DATA_AGENT_ID,
            label="Data Agent turn",
            plan=DATA_AGENT_ID,
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

    def _tools_with_kaggle(self, kind: str) -> ToolRegistry:
        """Kaggle（若接入）+ 共享会话的网页搜索/抓取。"""
        registry = ToolRegistry()
        kaggle = self._runtime.kaggle_tools(kind)
        if kaggle is not None:
            for spec in kaggle.specs:
                registry.register(kaggle.resolve(spec.name))
        # web_search 与 web_fetch 共享同一会话，使搜索结果 ref_id 可被
        # web_fetch 直接打开/查找（对齐 Codex web.run 的 open/find）。
        web_session = WebSession()
        registry.register(WebSearchTool(session=web_session))
        registry.register(WebFetchTool(session=web_session))
        return registry

    def _general_tools(self) -> ToolRegistry:
        """General Agent 的工具。"""
        return self._tools_with_kaggle("general")

    def _kaggle_handoff_tools(self) -> ToolRegistry:
        """Kaggle Handoff Agent 的工具。"""
        return self._tools_with_kaggle("kaggle_handoff")

    async def _ensure_kaggle_handoff(self) -> str:
        """Run the Kaggle Handoff Agent once and return its markdown text.

        Returns an empty string when Kaggle is disabled, the task is not a Kaggle
        competition, or the handoff could not be produced. Idea Generation must
        never block on this optional evidence channel.
        """
        rt = self._runtime
        if not getattr(rt._supervisor, "kaggle_enabled", False):
            return ""
        eda_dir = Path(self._resolve_eda_dir(rt))
        handoff_path = eda_dir / KAGGLE_HANDOFF_FILENAME
        if handoff_path.is_file():
            text = handoff_path.read_text(encoding="utf-8")[:MAX_KAGGLE_HANDOFF_CHARS]
            self._remember_handoff_ref("kaggle", await rt._store.put_text(text))
            return text
        task_text = (
            getattr(rt, "_task_text", "") or getattr(rt.state, "task_text", "") or ""
        )
        slug = kaggle_slug_from_task(task_text)
        if not slug:
            # Supervisor 对裸 slug（如 ``titanic``）也会开启 Kaggle；URL 解析
            # 拿不到时，从结构化任务理解的 dataset 字段取裸 slug 作为回退。
            understanding = getattr(rt.state, "task_understanding", None) or {}
            candidate = str(understanding.get("dataset") or "").strip()
            if (
                candidate
                and "/" not in candidate
                and not any(ch.isspace() for ch in candidate)
            ):
                slug = candidate
        if not slug or rt._provider is None:
            return ""
        try:
            if not rt._registry.contains(KAGGLE_HANDOFF_AGENT_TYPE):
                register_kaggle_handoff_agent(
                    rt._registry,
                    provider=rt._provider,
                    artifacts=rt._store,
                    workspace=eda_dir,
                    runtime=rt._execution,
                    extra_tools=self._kaggle_handoff_tools(),
                )
            content = (
                f"Competition slug: {slug}\n\n"
                f"Research task: {task_text}\n\n"
                "Read the EDA workspace's RESEARCH_HANDOFF.md, pull relevant Kaggle "
                "discussions and top notebooks, and write KAGGLE_HANDOFF.md."
            )
            if rt._agents.has_agent(KAGGLE_HANDOFF_AGENT_ID):
                run_id = await rt._agents.followup(
                    KAGGLE_HANDOFF_AGENT_ID, {"content": content, "context_refs": []}
                )
            else:
                _agent_id, run_id = await rt._agents.create_root(
                    KAGGLE_HANDOFF_AGENT_TYPE,
                    {"content": content, "context_refs": []},
                    agent_id=KAGGLE_HANDOFF_AGENT_ID,
                    name=KAGGLE_HANDOFF_AGENT_ID,
                )
            summary = await _wait_run_with_heartbeat(
                rt,
                rt._agents,
                run_id,
                agent_id=KAGGLE_HANDOFF_AGENT_ID,
                label="Kaggle handoff",
                plan=KAGGLE_HANDOFF_AGENT_ID,
            )
            result = await load_agent_result(summary, rt._store, KaggleHandoffResult)
            if handoff_path.is_file():
                if result is not None:
                    await rt.publish_output(
                        source="agent",
                        channel="text",
                        text=f"Kaggle handoff ready: {result.summary}",
                        plan=KAGGLE_HANDOFF_AGENT_ID,
                    )
                text = handoff_path.read_text(encoding="utf-8")[
                    :MAX_KAGGLE_HANDOFF_CHARS
                ]
                self._remember_handoff_ref("kaggle", await rt._store.put_text(text))
                return text
            message = (
                "Kaggle handoff agent finished without KAGGLE_HANDOFF.md"
                if result is not None
                else "Kaggle handoff agent failed"
            )
            await rt.publish_output(
                source="agent",
                channel="error",
                text=f"{message}; idea generation continues without Kaggle evidence.",
                plan=KAGGLE_HANDOFF_AGENT_ID,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - handoff 是可选证据通道
            await rt.publish_output(
                source="agent",
                channel="error",
                text=(
                    f"Kaggle handoff failed ({type(exc).__name__}: {exc}); "
                    "idea generation continues without Kaggle evidence."
                ),
                plan=KAGGLE_HANDOFF_AGENT_ID,
            )
        return ""

    @staticmethod
    def _lane_kwargs(handoff_texts: list[str], profile: IdeatorProfile | None) -> dict:
        """构造 _run_ideator_lane 的可选 kwargs，避免调用点出现 None 参数堆。"""
        kwargs: dict = {}
        if handoff_texts:
            kwargs["handoff_texts"] = handoff_texts
        if profile is not None:
            kwargs["profile"] = profile
        return kwargs

    @staticmethod
    def _ideator_allocations(count: int, lanes: int) -> tuple[int, ...]:
        """Distribute one requested batch across at most ``lanes`` ideator lanes."""
        if count <= 0 or lanes <= 0:
            return ()
        worker_count = min(lanes, count)
        base, remainder = divmod(count, worker_count)
        return tuple(base + (index < remainder) for index in range(worker_count))

    def _remember_handoff_ref(self, source: str, ref: str) -> None:
        """把 source -> artifact ref 记进 state，供断点续传复用。"""
        rt = self._runtime
        state = getattr(rt, "state", None)
        handoff_refs = getattr(state, "handoff_refs", None)
        if not isinstance(handoff_refs, dict):
            return
        if handoff_refs.get(source) == ref:
            return
        handoff_refs[source] = ref
        save = getattr(state, "save", None)
        state_path = getattr(rt, "_state_path", None)
        if save is not None and state_path:
            save(state_path)

    async def _collect_handoff_texts(self) -> list[str]:
        """按 state.handoff_sources 收集已启用的 handoff 文本。"""
        rt = self._runtime
        state = rt.state
        sources = getattr(state, "handoff_sources", None) or []
        texts: list[str] = []
        clarification_ref = getattr(state, "handoff_refs", {}).get("task_clarification")
        if clarification_ref:
            try:
                texts.append(await rt._store.get_text(clarification_ref))
            except Exception:  # noqa: BLE001 - 澄清记录是增益而非前提
                pass
        if "kaggle" in sources:
            text = await self._ensure_kaggle_handoff()
            if text:
                texts.append(text)
        return texts

    async def _run_ideator_lane(
        self,
        label: str,
        target: int,
        eda_dir: Path,
        *,
        handoff_texts: list[str] | None = None,
        profile: IdeatorProfile | None = None,
    ) -> HypothesisBatch:
        """Run one independent Ideator and return its structured batch."""
        rt = self._runtime
        content = (
            f"Inspect the EDA workspace at {eda_dir} without modifying any "
            "files, then propose up to "
            f"{target} falsifiable hypotheses that could improve the primary "
            "metric. Return the hypotheses as structured output."
        )
        if profile is not None:
            content += f"\n\n{profile.task_hint}"
        if handoff_texts:
            content += (
                "\n\nResearch handoff documents have been delivered to your "
                "mailbox; use them as supporting evidence."
            )
        context_refs: list[ArtifactRef] = []
        handoff = await read_eval_handoff(rt._store, rt._supervisor.evaluator_ref)
        if handoff:
            # 契约拼进 content。此前它只被塞进 context_refs 并在正文里声称"attached as
            # context"——而 context_refs 到不了 model，那句话一直是空头支票。
            context_refs.append(
                await rt._store.put_text(
                    json.dumps({"eval_handoff": handoff}, ensure_ascii=False)
                )
            )
            content += handoff_block(handoff)
        corpus_ref = rt.survey_corpus_ref()
        if corpus_ref is not None:
            content += await self._corpus_block(corpus_ref)
        request = {"content": content, "context_refs": context_refs}
        agent_type = profile.agent_type if profile is not None else "ideator"
        if handoff_texts:
            # 先注册 ideator 线程（不触发 turn），把 handoff 完成信息投进 mailbox，
            # 再启动首个 turn；BaseAgentRunner 会把未读 mailbox 消息追加进模型上下文。
            await rt._agents.resume_agent(label, agent_type=agent_type, name=label)
            mailbox_content = "\n\n".join(handoff_texts)
            await rt._agents.send_message(label, mailbox_content, [])
            agent_id, run_id = await rt._agents.create_root(
                agent_type, request, agent_id=label, name=label
            )
        else:
            agent_id, run_id = await rt._agents.create_root(
                agent_type, request, name=label
            )
        gated = getattr(rt, "_ideation", "ideageneration") == "ideageneration"
        schema = IdeatorHypothesisBatch if gated else HypothesisBatch

        # 门禁全拒时带理由重新提案：拒绝本身就是给生成侧的有效信号。上限是硬的——
        # 门禁若持续拒绝，无限重生成会变成死循环（真实跑测里 SEARCH 已因全拒而静默
        # 死过一次：返回空列表 -> generated=False -> run_search 直接 return -> 状态停在
        # RUNNING 既不推进也不终止）。
        for attempt in range(MAX_GATE_RETRIES + 1):
            summary = await _wait_run_with_heartbeat(
                rt,
                rt._agents,
                run_id,
                agent_id=agent_id,
                label=label,
                plan=label,
            )
            batch = await load_agent_result(summary, rt._store, schema)
            if batch is None:
                raise RuntimeError(summary.error or "Ideator turn failed")

            rejections: list[str] = []
            kept = await self._finish_ideator_batch(batch, rejections=rejections)
            if kept.hypotheses or not rejections or attempt == MAX_GATE_RETRIES:
                if not kept.hypotheses and rejections:
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

            regenerate = _regenerate_prompt(rejections, target)
            if profile is not None:
                regenerate += f"\n\n{profile.task_hint}"
            run_id = await rt._agents.followup(
                agent_id,
                {"content": regenerate, "context_refs": []},
            )
        raise AssertionError("unreachable: retry loop always returns")

    async def _finish_ideator_batch(
        self,
        batch: IdeatorHypothesisBatch | HypothesisBatch,
        *,
        rejections: list[str] | None = None,
    ) -> HypothesisBatch:
        """按消融模式决定 Ideator 产出如何进入 ResearchTree。

        ``ideageneration``：跑 Idea Generation 门禁（pre_gate + 视角审阅 +
        light_hard_gate），不合格的候选直接丢弃，不静默放行。
        ``baseline``：main 原有行为，产出即入库，作为消融对照组。
        两个模式统一返回 ``HypothesisBatch``，保留原批次的 ``eda_request``。
        """
        rt = self._runtime
        eda_request = getattr(batch, "eda_request", None)
        if getattr(rt, "_ideation", "ideageneration") != "ideageneration":
            return HypothesisBatch(
                hypotheses=await self._verify_sources(list(batch.hypotheses)),
                eda_request=eda_request,
            )

        async def progress(message: str) -> None:  # noqa: D401
            """把门禁进度投影成普通输出事件。

            门禁全程只有 LLM 往返、没有本地计算，不报进度的话外部无法区分"正在跑十几个
            调用"和"卡死了"。
            """
            publish = getattr(rt, "publish_output", None)
            if publish is not None:
                await publish(source="agent", channel="text", text=f"gate> {message}")

        kept = await run_light_pipeline(
            batch.hypotheses,
            model=rt._model,
            artifacts=rt._store,
            progress=progress,
            rejections=rejections,
        )
        return HypothesisBatch(
            hypotheses=await self._verify_sources(kept),
            eda_request=eda_request,
        )

    async def _corpus_block(self, corpus_ref: str) -> str:
        """把语料目录直接摆进 prompt，而不是指望 Agent 自己去调 overview。

        真机三次跑测里 Ideator **一次都没调过** ``paper_corpus_overview``，直接用泛词做
        语义检索；第 12 次因此从没碰过语料里那三篇真正讲 AUC 的论文——而任务主指标就是
        ROC-AUC。目录是纯索引读取（8 篇约 26 毫秒、1500 token），自己调一次比赌它会调
        便宜得多，也让"语料里有什么"成为确定的输入而不是运气。
        """
        papers = ""
        try:
            summaries = await self._runtime.corpus_summaries()
            papers = "\n".join(
                f"- {item.paper_id} — {item.title.strip() or '(untitled)'}"
                for item in summaries
            )
        except Exception:  # noqa: BLE001 - 目录读不出来不该拖垮 ideation
            logger.warning("corpus overview unavailable for the lane", exc_info=True)
        listing = f"\n\nIt holds these papers:\n{papers}" if papers else ""
        return (
            f"\n\nA literature corpus is available for this task "
            f"(corpus_ref={corpus_ref!r}).{listing}\n\n"
            "Use paper_semantic_search / paper_keyword_search to locate passages, then "
            "**paper_chunk_read to actually read them** — pick the papers whose "
            "subject matches this task's metric and data, not merely the topic. "
            "A hypothesis's "
            "`sources` must list only papers you opened with paper_chunk_read; "
            "citations to papers you never read are dropped and earn nothing."
        )

    async def _verify_sources(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        """只保留本轮**真正打开过正文**的论文，其余引用一律丢弃。

        ``ranker.rubric_prior`` 给"有引用"加 0.3（在总分里占 0.12），也就是说凭空写一个
        paper id 就能让候选往前排。这条奖励只有在引用可核验时才成立，否则它奖励的是幻觉。
        校验必须在入图之前做——进了图就是排序的输入了。

        **判据是"读过"，不是"在语料里"。** 第一版只查 id 是否存在于语料，真机（2026-08-16
        第 12 次）证明那太松：Ideator 拿《数据增强综述》支持"两两交互特征"、拿《信用卡欺诈
        检测综述》同时支持 target encoding 与 SMOTE，而语料里三篇真正讲 AUC 的论文一次都
        没被引用。带引用和不带引用的假设提的是同一批干预——引用是事后贴的标签，不是想法的
        来源。这些论文都在检索结果里出现过，只是从没被 ``paper_chunk_read`` 打开，所以
        "读过"能拦住而"存在"拦不住。

        没有语料时整段跳过：此时 ``sources`` 按 schema 本就该为空，不该顺手清掉别的来源
        写进去的内容。
        """
        rt = self._runtime
        if not await rt.corpus_paper_ids():
            return hypotheses
        opened = rt.corpus_papers_read()
        verified: list[Hypothesis] = []
        dropped = 0
        for hypothesis in hypotheses:
            kept = [source for source in hypothesis.sources if source in opened]
            dropped += len(hypothesis.sources) - len(kept)
            verified.append(hypothesis.model_copy(update={"sources": kept}))
        if dropped:
            await rt.publish_output(
                source="agent",
                channel="error",
                text=(
                    f"dropped {dropped} citation(s) to papers this round never opened; "
                    "cite only what you read with paper_chunk_read — a paper that "
                    "merely appeared in search results is not evidence"
                ),
            )
        return await self._verify_support(verified)

    async def _verify_support(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        """再问一层：被引的那几段正文，到底支不支持这条主张。

        ``_verify_sources`` 查的是**行为**（读没读过），这一层查的是**内容**。两者都需要：
        一个读过《数据增强综述》再拿它去支持"两两交互特征工程"的 Ideator，行为那一关是
        过得去的，而那正是真机上实际发生的事。

        判据保守——模棱两可算不支持。误删一条真引用只少了一份可追溯性；放行一条假引用会
        让下游把没有根据的干预当成有据可依，而这条链路已经为后者付过一次学费。

        判定失败（模型不可用、解析不出来）时**保留原引用**：这一层是增益，不该因为一次
        端点抖动就把整轮的引用清空。丢弃与保留的方向在这里是相反的——解析不出来判"不支持"
        是单条判定内部的保守，整层不可用则不该改变已经通过前一关的结论。
        """
        rt = self._runtime
        if rt._model is None or not any(item.sources for item in hypotheses):
            return hypotheses
        passages = await rt.corpus_passages_read()
        checked: list[Hypothesis] = []
        rejected: list[str] = []
        for hypothesis in hypotheses:
            if not hypothesis.sources:
                checked.append(hypothesis)
                continue
            verdicts = await asyncio.gather(
                *(
                    self._support_verdict(hypothesis, source, passages.get(source, []))
                    for source in hypothesis.sources
                ),
                return_exceptions=True,
            )
            kept: list[str] = []
            for source, verdict in zip(hypothesis.sources, verdicts):
                if isinstance(verdict, BaseException):
                    # 整层不可用 → 保留，别让端点抖动清空引用
                    kept.append(source)
                    continue
                if verdict.supports:
                    kept.append(source)
                else:
                    rejected.append(f"{source} ({verdict.why or 'no support'})")
            checked.append(hypothesis.model_copy(update={"sources": kept}))
        if rejected:
            await rt.publish_output(
                source="agent",
                channel="error",
                text=(
                    f"dropped {len(rejected)} citation(s) whose passages do not "
                    f"support the claim: {'; '.join(rejected[:5])}"
                ),
            )
        return checked

    async def _support_verdict(
        self, hypothesis: Hypothesis, paper_id: str, passages: list[str]
    ) -> SupportVerdict:
        """问一次"这段话支持这条主张吗"，返回结构化判定。"""
        rt = self._runtime
        prompt = SUPPORT_PROMPT.format(
            claim=hypothesis.statement,
            intervention=hypothesis.intervention,
            paper_id=paper_id,
            evidence=format_evidence(passages),
        )
        content = await single_turn_chat(
            prompt, model=rt._model, client=rt._client, max_tokens=200
        )
        return parse_verdict(paper_id, content)

    async def _run_debate_ideator_turn(
        self, count: int, handoff_texts: list[str] | None = None
    ) -> list[Hypothesis]:
        """Run the debate-based Ideator (proposal -> review -> revision -> judge).

        This is the pre-migration ideator preserved behind ``--ideation debate``. It
        consumes the shared ResearchTree directly and returns hypotheses in judge order;
        there is no pipeline-local ranking either.
        """
        from athena.agents.ideator import Ideator  # 延迟导入避免循环依赖
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
                if handoff_texts:
                    effective_prompt += "\n\n" + "\n\n".join(handoff_texts)
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
        # 组合根里没有现成来源；这里给最小画像 + 空文献/模型列表，保证模式可运行。
        profile = _DebateProfile()
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
        state = rt.state
        if state.task_research_ref is None and state.task_research_task in (None, task):
            changed = state.task_research_task is None
            if changed:
                # 断点续传：等待前先留下任务原文与稳定 id，worker 超时/进程崩溃后
                # 能按任务归属续跑同一线程；已有其他任务归属时绝不覆盖。
                state.task_research_task = task
            if state.task_research_agent_id != agent_id:
                state.task_research_agent_id = agent_id
                changed = True
            if changed:
                state.save(rt._state_path)
        summary = await _wait_run_with_heartbeat(
            rt,
            rt._agents,
            run_id,
            agent_id=agent_id,
            label="General Agent turn",
            plan="general",
        )
        result = await load_agent_result(summary, rt._store, GeneralResult)
        if result is None:
            raise RuntimeError(summary.error or "General Agent turn failed")
        return GeneralTurnOutcome(agent_id=agent_id, result=result.model_dump())
