"""生产 run_impl 构建器（设计方案2 §8：真实 Ideator/Code/Report 接入）。

真实业务模块需要模型凭据与执行沙箱,且其输入（profile/ResearchTree/实验
plan/worktree）来自项目状态而非 AgentContext。本模块用 ``ProjectState`` 协议
桥接：每个 turn 从项目状态解析输入,调用真实模块,把结果写为 Artifact 并返回
``AgentOutcome(result_ref)``。缺省仍使用确定性实现。
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from athena.core.agent.models import AgentContext, AgentOutcome
from athena.storage.artifact_store import ArtifactStore


@dataclass(frozen=True)
class IdeatorInputs:
    """真实 Ideator.generate 的输入（来自项目 PREPARE/ResearchTree）。"""

    profile: Any
    papers: list[Any]
    models: list[Any]
    tree: Any


@dataclass(frozen=True)
class CodeInputs:
    """真实 CodeAgent.execute 的输入（来自实验 plan 与 PREPARE 协议）。"""

    experiment_id: str
    hypothesis: Any
    plan: Any
    parent_commit: str
    eval_spec: Any
    worktree: Any
    inputs: Any


@dataclass(frozen=True)
class ReportInputs:
    """报告综合的已批准引用集合。"""

    refs: list[str]


class ProjectState(Protocol):
    """生产 run_impl 所需的项目状态输入解析。"""

    async def ideator_inputs(self, ctx: AgentContext) -> IdeatorInputs: ...
    async def code_inputs(self, ctx: AgentContext) -> CodeInputs: ...
    async def report_inputs(self, ctx: AgentContext) -> ReportInputs: ...


Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


def ideator_run_impl(ideator: Any, store: ArtifactStore, project: ProjectState) -> Impl:
    """构建真实 Ideator 的 run_impl：委托 :class:`~athena.agents.ideator_agent.IdeatorAgent`。

    真实 Ideator 的接入逻辑（解析项目输入 → ``generate`` → 写 ``DebateResult``）
    收敛在 ``IdeatorAgent``；这里只构造接入了 ``ideator`` + ``project`` 的实例并
    返回其 ``run``，保持既有注入契约。
    """
    from athena.agents.ideator_agent import IdeatorAgent

    return IdeatorAgent(store, ideator=ideator, project=project).run


def code_run_impl(code_agent: Any, store: ArtifactStore, project: ProjectState) -> Impl:
    """构建真实 CodeAgent 的 run_impl：解析实验输入 → execute → 写结果。"""

    async def _run(ctx: AgentContext) -> AgentOutcome:
        inputs = await project.code_inputs(ctx)
        result = await code_agent.execute(
            inputs.experiment_id,
            inputs.hypothesis,
            inputs.plan,
            inputs.parent_commit,
            inputs.eval_spec,
            inputs.worktree,
            inputs=inputs.inputs,
        )
        result_ref = await store.put_text(_json(result.model_dump(mode="json")))
        return AgentOutcome(result_ref=result_ref)

    return _run


def report_run_impl(reporter: Any, store: ArtifactStore, project: ProjectState) -> Impl:
    """构建真实报告生成的 run_impl：解析已批准引用 → 综合 → 写报告。"""

    async def _run(ctx: AgentContext) -> AgentOutcome:
        inputs = await project.report_inputs(ctx)
        report = await reporter.generate(inputs.refs)
        result_ref = await store.put_text(_json(report))
        return AgentOutcome(result_ref=result_ref)

    return _run


def _json(payload: object) -> str:
    """把结果序列化为可读 JSON（含非 JSON 默认字段的兜底）。"""
    return json.dumps(payload, ensure_ascii=False, default=str)
