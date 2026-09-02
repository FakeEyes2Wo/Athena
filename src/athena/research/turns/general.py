"""General/Kaggle turn mixin for ``AgentTurnRunner``.

Keeps the General Agent and optional Kaggle-handoff machinery out of the main
runner so the runner is easier to read.
"""

import asyncio
import traceback
from pathlib import Path
from typing import Any

from athena.agents.kaggle_handoff_agent import (
    KAGGLE_HANDOFF_AGENT_ID,
    KAGGLE_HANDOFF_AGENT_TYPE,
    KAGGLE_HANDOFF_FILENAME,
    KaggleHandoffResult,
    register_kaggle_handoff_agent,
)
from athena.agents.task_agents import GeneralResult, register_general_agent
from athena.core.tool import ToolRegistry
from athena.kaggle.wiring import kaggle_slug_from_task
from athena.research.turns.common import (
    MAX_KAGGLE_HANDOFF_CHARS,
    wait_run_with_heartbeat,
)
from athena.research.contracts import GeneralTurnOutcome
from athena.research.supervisor.experiment import load_agent_result
from athena.retrieval.web_search import WebFetchTool, WebSearchTool, WebSession


class GeneralTurnMixin:
    """Mixin providing General Agent and Kaggle-handoff turns."""

    _runtime: Any
    _resolve_eda_dir: Any
    _remember_handoff_ref: Any

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
        if not getattr(rt.supervisor, "kaggle_enabled", False):
            return ""
        eda_dir = Path(self._resolve_eda_dir(rt))
        handoff_path = eda_dir / KAGGLE_HANDOFF_FILENAME
        if handoff_path.is_file():
            text = handoff_path.read_text(encoding="utf-8")[:MAX_KAGGLE_HANDOFF_CHARS]
            self._remember_handoff_ref("kaggle", await rt.store.put_text(text))
            return text
        task_text = (
            getattr(rt, "task_text", "") or getattr(rt.state, "task_text", "") or ""
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
        if not slug or rt.provider is None:
            return ""
        try:
            if not rt.registry.contains(KAGGLE_HANDOFF_AGENT_TYPE):
                register_kaggle_handoff_agent(
                    rt.registry,
                    provider=rt.provider,
                    artifacts=rt.store,
                    workspace=eda_dir,
                    runtime=rt.execution,
                    extra_tools=self._kaggle_handoff_tools(),
                )
            content = (
                f"Competition slug: {slug}\n\n"
                f"Research task: {task_text}\n\n"
                "Read the EDA workspace's RESEARCH_HANDOFF.md, pull relevant Kaggle "
                "discussions and top notebooks, and write KAGGLE_HANDOFF.md."
            )
            if rt.agents.has_agent(KAGGLE_HANDOFF_AGENT_ID):
                run_id = await rt.agents.followup(
                    KAGGLE_HANDOFF_AGENT_ID, {"content": content, "context_refs": []}
                )
            else:
                _agent_id, run_id = await rt.agents.create_root(
                    KAGGLE_HANDOFF_AGENT_TYPE,
                    {"content": content, "context_refs": []},
                    agent_id=KAGGLE_HANDOFF_AGENT_ID,
                    name=KAGGLE_HANDOFF_AGENT_ID,
                )
            summary = await wait_run_with_heartbeat(
                rt,
                rt.agents,
                run_id,
                agent_id=KAGGLE_HANDOFF_AGENT_ID,
                label="Kaggle handoff",
                plan=KAGGLE_HANDOFF_AGENT_ID,
            )
            result = await load_agent_result(summary, rt.store, KaggleHandoffResult)
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
                self._remember_handoff_ref("kaggle", await rt.store.put_text(text))
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
                    "idea generation continues without Kaggle evidence.\n\n"
                    f"{traceback.format_exc()}"
                ),
                plan=KAGGLE_HANDOFF_AGENT_ID,
            )
        return ""

    async def run_general_turn(
        self, task: str, prior_agent_id: str | None = None
    ) -> GeneralTurnOutcome:
        """Dispatch one General Agent rooted at the project and return its outcome.

        ``prior_agent_id`` 续跑同一个 worker：进程重启后经其 rollout 恢复记忆，
        避免断点续传时重复调研。
        """
        rt = self._runtime
        if rt.provider is None:
            raise RuntimeError("General Agent requires a registered Agent provider")
        if not rt.registry.contains("general"):
            register_general_agent(
                rt.registry,
                provider=rt.provider,
                artifacts=rt.store,
                project_root=rt.root,
                runtime=rt.execution,
                extra_tools=self._general_tools(),
            )
        request = {"content": task, "context_refs": []}
        if prior_agent_id is not None and rt.agents.has_agent(prior_agent_id):
            agent_id = prior_agent_id
            run_id = await rt.agents.followup(agent_id, request)
        else:
            # ``agent_id=None`` 时新开随机 worker；给定 prior id 则经 rollout 恢复记忆。
            agent_id, run_id = await rt.agents.create_root(
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
                state.save(rt.state_path)
        summary = await wait_run_with_heartbeat(
            rt,
            rt.agents,
            run_id,
            agent_id=agent_id,
            label="General Agent turn",
            plan="general",
        )
        result = await load_agent_result(summary, rt.store, GeneralResult)
        if result is None:
            raise RuntimeError(summary.error or "General Agent turn failed")
        return GeneralTurnOutcome(agent_id=agent_id, result=result.model_dump())
