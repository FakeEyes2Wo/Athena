"""生产 run_impl 构建器：Ideator 结构化输出与 Code 工作区执行。

Ideator 以 ``HypothesisBatch`` 结构化输出（prompt=ideator_agent.md，无命令工具）；
Code 在分配工作区内用 ``shell_command`` 产出代码/预测/报告，并要求
``model.py``/``predictions.csv``/``REPORT.md`` 三件套齐全。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from athena.agents.prompt_agent import build_llm_agent, load_prompt
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.provider import create_provider
from athena.core.agent.runtime import Agent
from athena.core.contracts import ArtifactStore
from athena.core.research_models import HypothesisBatch
from athena.core.tool import ToolRegistry
from athena.execution.runtime import ExecutionRuntime

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


@dataclass(frozen=True)
class IdeatorInputs:
    """Inputs resolved from project state for the debate-based Ideator."""

    profile: Any
    papers: list[Any]
    models: list[Any]
    tree: Any


class IdeatorProject(Protocol):
    async def ideator_inputs(self, ctx: AgentContext) -> IdeatorInputs: ...


def ideator_run_impl(
    ideator: Any, store: ArtifactStore, project: IdeatorProject
) -> Impl:
    """Adapt the debate-based Ideator to the registered Agent run contract."""

    async def _run(ctx: AgentContext) -> AgentOutcome:
        inputs = await project.ideator_inputs(ctx)
        result = await ideator.generate(
            inputs.profile,
            inputs.papers,
            inputs.models,
            inputs.tree,
        )
        result_ref = await store.put_text(
            json.dumps(
                result.model_dump(mode="json"),
                ensure_ascii=False,
                default=str,
            )
        )
        return AgentOutcome(result_ref=result_ref)

    return _run


def structured_ideator_run_impl(
    store: ArtifactStore, *, model: str, client: Any
) -> Impl:
    """结构化 Ideator run_impl：核心 Agent 以 HypothesisBatch 结构化输出。

    prompt=ideator_agent.md（无命令工具）；输入 JSON 含 task/EDA/graph/父 SOTA
    上下文。产出 HypothesisBatch artifact，供 ``search.register_hypotheses`` 消费。
    """

    async def _run(ctx: AgentContext) -> AgentOutcome:
        inner = Agent(
            create_provider(model, client=client),
            ToolRegistry(),
            load_prompt("ideator"),
            output_type=HypothesisBatch,
            artifacts=store,
        )
        inner_ctx = AgentContext(
            thread=ctx.thread,
            turn=ctx.turn,
            emit=ctx.emit,
            tools=inner.tools,
            cancel=ctx.cancel,
            memory=ctx.memory,
            input_text=ctx.input_text,
        )
        return await inner.run(inner_ctx)

    return _run


def code_run_impl(
    store: ArtifactStore,
    *,
    model: str,
    client: Any,
    project_root: str | Path,
) -> Impl:
    """workspace-aware Code run_impl：在分配的工作区里用 shell_command 产出代码/预测/报告。

    assignment 由 ``search.prepare_experiment`` 产出（experiment_id/workspace/
    environment_root/hypothesis）。要求 ``model.py``/``predictions.csv``/``REPORT.md``；
    结果只含路径与 experiment_id，不接收 labels 或 score（design 修复 5）。
    """

    async def _run(ctx: AgentContext) -> AgentOutcome:
        assignment = json.loads(ctx.input_text or "{}")
        workspace = Path(assignment.get("workspace") or "")
        if not workspace.is_dir():
            raise RuntimeError(f"code assignment workspace missing: {workspace}")
        runtime = ExecutionRuntime(
            project_root=project_root,
            environment_root=workspace,
            store=store,
        )
        inner = build_llm_agent(
            "code",
            model=model,
            client=client,
            workspace=workspace,
            runtime=runtime,
        )
        prompt = json.dumps(assignment, ensure_ascii=False)
        required = ("model.py", "predictions.csv", "REPORT.md")
        while True:
            inner_ctx = AgentContext(
                thread=ctx.thread,
                turn=ctx.turn,
                emit=ctx.emit,
                tools=inner.tools,
                cancel=ctx.cancel,
                memory=ctx.memory,
                input_text=prompt,
            )
            await inner.run(inner_ctx)
            missing = [name for name in required if not (workspace / name).is_file()]
            if not missing:
                break
            if ctx.cancel.is_set():
                raise asyncio.CancelledError
            prompt = (
                "The previous attempt did not satisfy the output contract. "
                f"Missing files: {', '.join(missing)}. Inspect the existing workspace "
                "and create the missing files now. Do not continue exploration or "
                "hyperparameter tuning before all required files exist."
            )
        result_ref = await store.put_text(
            json.dumps(
                {
                    "experiment_id": assignment.get("experiment_id"),
                    "workspace": str(workspace),
                    "predictions_path": str(workspace / "predictions.csv"),
                    "report_path": str(workspace / "REPORT.md"),
                },
                ensure_ascii=False,
            )
        )
        return AgentOutcome(result_ref=result_ref)

    return _run
