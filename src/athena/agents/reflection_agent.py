"""只读评审 Agent：DatasetRoleReview / EDAReview 的结构化决策（supervisor_design §2.4）。

请求带 ``data_analysis_ref``（角色提议 / EDA 报告）时评审对应产物，输出与真实 LLM
路径同合同的扁平 JSON：:

    {"decision": "ACCEPT" | "REVISE", "issues": [], "required_changes": [],
     "evidence_refs": []}

不能修改被评产物，也不能提交新的版本。首版为确定性实现：按报告非空与图表存在
派生决策，供 Supervisor 的 ACCEPT/REVISE 闭环与本地测试使用。
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from athena.agents.prompt_agent import load_prompt
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.provider import ResponsesProvider
from athena.core.agent.runtime import Agent, BaseAgent
from athena.core.contracts import ArtifactStore
from athena.core.bundle import DirectoryBundle, InvalidBundleError
from athena.core.tool import ToolRegistry
from athena.research.contracts import DatasetRoleReview, EDAReview

Impl = Callable[[AgentContext], Awaitable[AgentOutcome]]


class ReflectionAgent(BaseAgent):
    """只读评审 Agent：DatasetRoleReview / EDAReview 的 LLM 决策（Task 5）。

    注入 ``run_impl`` 时按 reflection_agent.md prompt 输出结构化 ACCEPT/REVISE
    决策（真实 LLM 由 composition root 接入）；缺省为确定性评审，派生 decision
    字段（report 非空、图表存在），与 run_impl 合同一致。
    """

    def __init__(self, store: ArtifactStore, *, run_impl: Impl | None = None) -> None:
        self._store = store
        self._run_impl = run_impl

    async def run(self, ctx: AgentContext) -> AgentOutcome:
        """按请求评审对象；注入 run_impl 时委托真实 LLM 评审。"""
        if self._run_impl is not None:
            return await self._run_impl(ctx)
        request = json.loads(ctx.input_text or "{}")
        source_ref = request.get("data_analysis_ref") or request.get("report_ref")
        report_only = request.get("report_ref") is not None
        kind = str(request.get("kind", "eda"))
        return await self._review(source_ref, report_only=report_only, kind=kind)

    async def _review(
        self, source_ref: str | None, *, report_only: bool, kind: str = "eda"
    ) -> AgentOutcome:
        """确定性评审：读取被评产物并派生 ACCEPT/REVISE 决策。

        两阶段合同（supervisor_design §2.4）：``role`` 评审读 ``dataset_role_proposal.json``
        （proposal 是角色提议的产物，不要求图表）；``eda`` 评审读 ``report.md`` 且
        要求至少一张图。报告/产物为空或缺失 → REVISE。evidence_refs 指向实际读取的
        report/图表，供 HumanRequest 展示与 Validator 引用。
        """
        report = ""
        evidence_refs: list[str] = []
        has_figure = False
        if source_ref is not None:
            try:
                files = await DirectoryBundle.files(self._store, source_ref)
            except (InvalidBundleError, json.JSONDecodeError):
                files = {}
                report = await self._store.get_text(source_ref)
                evidence_refs.append(source_ref)
            if kind == "role":
                proposal_ref = files.get("dataset_role_proposal.json")
                if proposal_ref is not None:
                    report = await self._store.get_text(proposal_ref)
                    evidence_refs.append(proposal_ref)
            else:
                report_ref = files.get("report.md")
                if report_ref is not None:
                    report = await self._store.get_text(report_ref)
                    evidence_refs.append(report_ref)
                figures = [p for p in files if p.startswith("figures/")]
                if figures:
                    has_figure = True
                    evidence_refs.append(files[figures[0]])
        issues: list[str] = []
        if not report.strip():
            issues.append("report 为空或缺失")
        if kind != "role" and not report_only and not has_figure:
            issues.append("缺少图表（figures/*）")
        payload = {
            "decision": "ACCEPT" if not issues else "REVISE",
            "issues": issues,
            "required_changes": issues,
            "evidence_refs": evidence_refs,
        }
        review_ref = await self._store.put_text(json.dumps(payload, ensure_ascii=False))
        return AgentOutcome(result_ref=review_ref)


async def _load_target_text(
    store: ArtifactStore, ref: str | None, *, kind: str = "eda"
) -> str:
    """读取被评产物文本：role 评审优先 proposal JSON，EDA 优先 report.md。

    两阶段合同：角色提议（``kind="role"``）读 ``dataset_role_proposal.json``；
    EDA 评审（``kind="eda"``）读 ``report.md``。否则按文本 Artifact 读取。
    """
    if ref is None:
        return ""
    try:
        files = await DirectoryBundle.files(store, ref)
        if kind == "role":
            for name in ("dataset_role_proposal.json", "report.md"):
                if name in files:
                    return await store.get_text(files[name])
        else:
            for name in ("report.md", "dataset_role_proposal.json", "eda_report.md"):
                if name in files:
                    return await store.get_text(files[name])
    except Exception:
        pass
    try:
        return await store.get_text(ref)
    except Exception:
        return ""


def build_reflection_run_impl(
    store: ArtifactStore,
    *,
    model: str,
    client: Any = None,
    agent_builder: Callable[..., Agent] | None = None,
) -> Impl:
    """装配真实 LLM 评审 run_impl（supervisor_design §2.4）。

    ``client`` 为 None（未配置真实 LLM）时退化为确定性评审，保证本地测试不 hit
    API；配置 client 后按 reflection_agent.md 用结构化输出让内层 LLM 判断被评
    产物，产出与确定性 fallback 同合同的扁平 decision payload。输出合同按请求
    里的 ``kind`` 选择：``eda`` → EDAReview，否则 DatasetRoleReview。
    ``agent_builder`` 为测试接缝（注入 fake provider 验证结构化路径）。
    """

    async def run_impl(ctx: AgentContext) -> AgentOutcome:
        request = json.loads(ctx.input_text or "{}")
        source_ref = request.get("data_analysis_ref") or request.get("report_ref")
        report_only = request.get("report_ref") is not None
        kind = str(request.get("kind", "eda"))
        if client is None:
            return await ReflectionAgent(store)._review(
                source_ref, report_only=report_only, kind=kind
            )
        target = await _load_target_text(store, source_ref, kind=kind)
        output_type = EDAReview if request.get("kind") == "eda" else DatasetRoleReview
        if agent_builder is not None:
            inner = agent_builder(
                model=model, client=client, output_type=output_type, artifacts=store
            )
        else:
            inner = Agent(
                ResponsesProvider(model, client=client),
                ToolRegistry(),
                load_prompt("reflection"),
                output_type=output_type,
                artifacts=store,
            )
        inner_ctx = AgentContext(
            thread=ctx.thread,
            turn=ctx.turn,
            emit=ctx.emit,
            tools=inner.tools,
            cancel=ctx.cancel,
            memory=ctx.memory,
            input_text=json.dumps(
                {"request": request.get("content", ""), "target": target},
                ensure_ascii=False,
            ),
        )
        return await inner.run(inner_ctx)

    return run_impl
