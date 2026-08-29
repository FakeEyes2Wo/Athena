"""Agent turn execution for the research runtime (Supervisor / Ideator / General).

拆自 ``ResearchRuntime``：把"运行一个 Agent turn 并解包结构化结果"的逻辑
集中到 ``AgentTurnRunner``。持有 ``runtime`` 引用访问组合根的共享基础设施。
"""

import asyncio
import itertools
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from athena.agents.task_agents import DATA_AGENT_ID, register_data_agent
from athena.agents.ideator_agent import (
    SEARCH_IDEATOR_PROFILES,
    IdeatorProfile,
    register_ideator_agent,
)
from athena.agents.supervisor_agent import SUPERVISOR_AGENT_ID, SupervisorAnswer
from athena.core.contracts import ArtifactRef
from athena.core.research_models import EdaResult, Hypothesis, HypothesisBatch
from athena.research.agent_turn_common import (
    AGENT_TURN_TIMEOUT_SECONDS,
    regenerate_prompt as _regenerate_prompt,
    wait_run_with_heartbeat as _wait_run_with_heartbeat,
)
from athena.research.agent_turn_general import GeneralTurnMixin
from athena.research.agent_turn_support import SupportVerificationMixin
from athena.research.idea_generation.gate import run_light_pipeline
from athena.research.idea_generation.idea_schemas import (
    IdeatorHypothesisBatch,
    IdeatorHypothesisDraft,
)
from athena.research.rubrics.models import EvaluationPolicy
from athena.research.rubrics.workflow import RubricWorkflow
from athena.research.supervisor.experiment import (
    handoff_block,
    load_agent_result,
    read_eval_handoff,
)

if TYPE_CHECKING:
    from athena.research.runtime import ResearchRuntime

logger = logging.getLogger(__name__)

# Compatibility for callers that imported this helper before Rubric orchestration
# moved into its own maintainable module.
_read_eval_handoff = read_eval_handoff


MAX_GATE_RETRIES = 2
"""门禁全拒后最多重新提案几次。

上限是硬的：门禁若持续拒绝，无限重生成就是死循环。取 2 是保守起点——一次让生成侧
按理由修正，一次留给它换个方向；没有经验依据，跑过几轮真实搜索后再校准。
"""


class _DebateProfile(BaseModel):
    """辩论 Ideator 的最小数据画像（EDA-only SEARCH 没有完整 DataProfile 来源）。"""

    row_count: int = 0
    col_count: int = 0
    task_type_hint: str = "eda_workspace"


class AgentTurnRunner(GeneralTurnMixin, SupportVerificationMixin):
    """Run Supervisor / Ideator / General Agent turns via the runtime's infra."""

    def __init__(self, runtime: "ResearchRuntime") -> None:
        self._runtime = runtime
        self._ideator_round = 0
        self._rubrics = RubricWorkflow(
            runtime, eda_root=lambda: Path(self._resolve_eda_dir(runtime))
        )

    async def run_supervisor_turn(self, text: str) -> str:
        """Run one serialized SupervisorAgent turn and return its human-facing answer."""
        rt = self._runtime
        if rt.provider is None:
            raise RuntimeError("SupervisorAgent provider is not registered")
        request = {"content": text, "context_refs": []}
        if rt.agents.has_agent(SUPERVISOR_AGENT_ID):
            run_id = await rt.agents.followup(SUPERVISOR_AGENT_ID, request)
        else:
            _agent_id, run_id = await rt.agents.create_root(
                "supervisor",
                request,
                agent_id=SUPERVISOR_AGENT_ID,
                name=SUPERVISOR_AGENT_ID,
            )
        summary = await _wait_run_with_heartbeat(
            rt,
            rt.agents,
            run_id,
            agent_id=SUPERVISOR_AGENT_ID,
            label="SupervisorAgent turn",
            plan=SUPERVISOR_AGENT_ID,
            project=False,
        )
        result = await load_agent_result(summary, rt.store, SupervisorAnswer)
        if result is None:
            raise RuntimeError(summary.error or "SupervisorAgent turn failed")
        await rt.publish_output(source="supervisor", channel="text", text=result.answer)
        return result.answer

    async def run_evaluation_rubric(self) -> tuple[EvaluationPolicy, ArtifactRef]:
        """Delegate Layer 1 to the isolated Rubric workflow."""
        return await self._rubrics.run_evaluation()

    async def run_hypothesis_rubric(
        self, hypotheses: list[Hypothesis]
    ) -> list[Hypothesis]:
        """Delegate the single post-Gate batch review."""
        if getattr(self._runtime, "_ideation", "ideageneration") != "ideageneration":
            return hypotheses
        return await self._rubrics.run_hypothesis_priority(hypotheses)

    @staticmethod
    def _resolve_eda_dir(rt: "ResearchRuntime") -> str:
        """把 ``state.eda_dir`` 解析为本项目内的绝对 EDA 目录并做存在性校验。"""
        eda_dir = rt.state.eda_dir
        if not eda_dir:
            # PREPARE 失败后 state.eda_dir 可能为空，但默认 EDA 目录已建好；
            # 只要目录存在就继续，不因为状态字段缺失而误报“未捕获”。
            default_eda = getattr(rt, "workspaces_root", None)
            if default_eda is not None and os.path.exists(Path(default_eda) / "eda"):
                eda_dir = str(Path(default_eda) / "eda")
            else:
                raise RuntimeError("EDA workspace not captured; PREPARE must run first")
        eda_path = Path(eda_dir)
        # 相对项目根的路径（新契约）解析为绝对；旧 state 遗留的绝对路径原样保留。
        if not eda_path.is_absolute():
            eda_path = (rt.root / eda_dir).resolve()
        root = getattr(rt, "root", None)
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
        if rt.provider is None:
            raise RuntimeError("Ideator requires a registered Agent provider")
        # 按 state.handoff_sources 收集启用的 handoff；失败来源只返回空文本，
        # 不影响本地 EDA-only 的 idea generation。
        handoff_texts = await self._collect_handoff_texts()
        if getattr(rt, "ideation", "ideageneration") == "debate":
            return await self._run_debate_ideator_turn(count, handoff_texts)
        eda_dir = self._resolve_eda_dir(rt)
        ideation = getattr(rt, "ideation", "ideageneration")
        ideator_provider = getattr(rt, "reasoning_provider", None) or rt.provider
        lane_profiles: Iterator[IdeatorProfile | None]
        if ideation == "ideageneration":
            for profile in SEARCH_IDEATOR_PROFILES:
                if not rt.registry.contains(profile.agent_type):
                    register_ideator_agent(
                        rt.registry,
                        provider=ideator_provider,
                        structured_repair_provider=rt.provider,
                        artifacts=rt.store,
                        workspace=Path(eda_dir),
                        runtime=rt.execution,
                        extra_tools=rt.ideator_tools(),
                        gated=True,
                        profile=profile,
                    )
            lane_profiles = itertools.cycle(SEARCH_IDEATOR_PROFILES)
        else:
            if not rt.registry.contains("ideator"):
                register_ideator_agent(
                    rt.registry,
                    provider=ideator_provider,
                    structured_repair_provider=rt.provider,
                    artifacts=rt.store,
                    workspace=Path(eda_dir),
                    runtime=rt.execution,
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
        events = getattr(rt, "events", None)
        if events is not None:
            events.set_ideator_lanes(len(allocations))
            await events.publish_ideator_state()
        lane_results = await asyncio.gather(
            *(
                self._run_ideator_lane(
                    f"ideator-{round_label}-{index}",
                    target,
                    Path(eda_dir),
                    handoff_texts=handoff_texts,
                    profile=next(lane_profiles),
                )
                for index, target in enumerate(allocations, start=1)
            ),
            return_exceptions=True,
        )
        hypotheses: list[Hypothesis] = []
        eda_requests: list[str] = []
        lane_failures: list[str] = []
        for index, result in enumerate(lane_results, start=1):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failure = " ".join(str(result).split())[:500]
                lane_failures.append(f"lane {index}: {failure}")
                await rt.publish_output(
                    source="agent",
                    channel="error",
                    text=f"Ideator {index} failed: {failure}",
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
        if not hypotheses:
            detail = "; ".join(lane_failures) or "all candidates failed the Gate"
            raise RuntimeError(
                "Ideator batch produced no valid hypotheses; SEARCH cannot "
                f"continue ({detail})"
            )
        # 返回全部产物（不做截断）：所有生成假设都进入 graph，由调度器排序后按
        # 并发度逐个启动。单个 lane 失败不会丢弃成功 peer 的结果。
        return hypotheses

    async def run_data_turn(self, request: str) -> None:
        """派发 Data Agent 在 EDA 目录上做补充分析并写回 report/figures。"""
        rt = self._runtime
        if rt.provider is None:
            raise RuntimeError("Data Agent requires a registered Agent provider")
        eda_dir = self._resolve_eda_dir(rt)
        if not rt.registry.contains("data"):
            register_data_agent(
                rt.registry,
                provider=rt.provider,
                artifacts=rt.store,
                workspace=Path(eda_dir),
                runtime=rt.execution,
            )
        await rt.publish_output(
            source="agent",
            channel="text",
            text=f"补充 EDA 请求：{request}",
            plan=DATA_AGENT_ID,
        )
        task = {"content": request, "context_refs": []}
        if rt.agents.has_agent(DATA_AGENT_ID):
            run_id = await rt.agents.followup(DATA_AGENT_ID, task)
        else:
            _agent_id, run_id = await rt.agents.create_root(
                "data",
                task,
                agent_id=DATA_AGENT_ID,
                name=DATA_AGENT_ID,
            )
        summary = await _wait_run_with_heartbeat(
            rt,
            rt.agents,
            run_id,
            agent_id=DATA_AGENT_ID,
            label="Data Agent turn",
            plan=DATA_AGENT_ID,
        )
        result = await load_agent_result(summary, rt.store, EdaResult)
        if result is None:
            raise RuntimeError(summary.error or "Data Agent turn failed")
        await rt.publish_output(
            source="agent",
            channel="text",
            text=f"补充 EDA 完成：{result.summary}",
            plan=DATA_AGENT_ID,
        )

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
        state_path = getattr(rt, "state_path", None)
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
                texts.append(await rt.store.get_text(clarification_ref))
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
            "files, then propose between 1 and "
            f"{target} distinct, falsifiable hypotheses that could improve the "
            "primary metric. Return the hypotheses as structured output. Never "
            "return an empty hypotheses list; use the available task and EDA "
            "evidence even when no further tool call is necessary."
        )
        if profile is not None:
            content += f"\n\n{profile.task_hint}"
        policy = getattr(rt._supervisor, "evaluation_policy", None)
        if policy is not None:
            content += (
                "\n\nThe frozen Evaluation Policy is authoritative:\n"
                + json.dumps(policy.model_dump(mode="json"), ensure_ascii=False)
            )
        if handoff_texts:
            content += (
                "\n\nResearch handoff documents have been delivered to your "
                "mailbox; use them as supporting evidence."
            )
        context_refs: list[ArtifactRef] = []
        handoff = await read_eval_handoff(rt.store, rt.supervisor.evaluator_ref)
        if handoff:
            # 契约拼进 content。此前它只被塞进 context_refs 并在正文里声称"attached as
            # context"——而 context_refs 到不了 model，那句话一直是空头支票。
            context_refs.append(
                await rt.store.put_text(
                    json.dumps({"eval_handoff": handoff}, ensure_ascii=False)
                )
            )
            content += handoff_block(handoff)
        corpus_ref = rt.survey_corpus_ref()
        if corpus_ref is not None:
            content += await self._corpus_block(corpus_ref)
        request = {"content": content, "context_refs": context_refs}
        agent_type = profile.agent_type if profile is not None else "ideator"
        agent_id: str | None = None
        try:
            if handoff_texts:
                # 先注册 ideator 线程（不触发 turn），把 handoff 完成信息投进 mailbox，
                # 再启动首个 turn；BaseAgentRunner 会把未读 mailbox 消息追加进模型上下文。
                await rt.agents.resume_agent(label, agent_type=agent_type, name=label)
                mailbox_content = "\n\n".join(handoff_texts)
                await rt.agents.send_message(label, mailbox_content, [])
                agent_id, run_id = await rt.agents.create_root(
                    agent_type, request, agent_id=label, name=label
                )
            else:
                agent_id, run_id = await rt.agents.create_root(
                    agent_type, request, name=label
                )
            gated = getattr(rt, "ideation", "ideageneration") == "ideageneration"
            schema = IdeatorHypothesisBatch if gated else HypothesisBatch

            # 门禁全拒时带理由重新提案：拒绝本身就是给生成侧的有效信号。上限是硬的——
            # 门禁若持续拒绝，无限重生成会变成死循环（真实跑测里 SEARCH 已因全拒而静默
            # 死过一次：返回空列表 -> generated=False -> run_search 直接 return -> 状态停在
            # RUNNING 既不推进也不终止）。
            for attempt in range(MAX_GATE_RETRIES + 1):
                summary = await _wait_run_with_heartbeat(
                    rt,
                    rt.agents,
                    run_id,
                    agent_id=agent_id,
                    label=label,
                    plan=label,
                )
                batch = await load_agent_result(summary, rt.store, schema)
                if batch is None:
                    raise RuntimeError(summary.error or "Ideator turn failed")

                rejections: list[str] = []
                kept = await self._finish_ideator_batch(
                    cast(IdeatorHypothesisBatch | HypothesisBatch, batch),
                    rejections=rejections,
                )
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
                run_id = await rt.agents.followup(
                    agent_id,
                    {"content": regenerate, "context_refs": []},
                )
            raise AssertionError("unreachable: retry loop always returns")
        finally:
            # Ideator lanes are per-round one-shot workers. Reap after the lane
            # finishes (success, failure, or cancellation) so consecutive rounds
            # do not accumulate closed threads/rollout metadata.
            if agent_id is not None:
                try:
                    await rt.agents.reap(agent_id)
                except Exception:  # noqa: BLE001,S110 - GC must never mask lane failure
                    pass

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
        if getattr(rt, "ideation", "ideageneration") != "ideageneration":
            return HypothesisBatch(
                hypotheses=await self._verify_sources(
                    cast(list[Hypothesis], batch.hypotheses)
                ),
                eda_request=eda_request,
            )

        async def progress(message: str) -> None:
            """把门禁进度投影成普通输出事件。

            门禁全程只有 LLM 往返、没有本地计算，不报进度的话外部无法区分"正在跑十几个
            调用"和"卡死了"。
            """
            publish = getattr(rt, "publish_output", None)
            if publish is not None:
                await publish(source="agent", channel="text", text=f"gate> {message}")

        kept = await run_light_pipeline(
            cast(list[IdeatorHypothesisDraft], batch.hypotheses),
            model=rt.model,
            artifacts=rt.store,
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
        except Exception:
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
                    model=rt.model,
                    artifacts=rt.store,
                    tools=debate_tools,
                )
                return _StructuredResult(value)

        def agent_factory(_role: str, _agent_index: int):
            """Return one debate agent adapter for a role lane."""
            return _DebateAgentAdapter()

        # 辩论 Ideator 的完整输入（DataProfile/papers/models）在 EDA-only 的 SEARCH
        # 组合根里没有现成来源；这里给最小画像 + 空文献/模型列表，保证模式可运行。
        profile = _DebateProfile()
        ideator = Ideator(agent_factory=agent_factory, artifacts=rt.store)
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
