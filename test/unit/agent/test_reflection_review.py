"""ReflectionAgent LLM 评审路径测试（supervisor_imp_docs Task 5）。

注入 fake ``run_impl`` 验证 DatasetRoleReview / EDAReview 决策产出；确定性
fallback 路径由 test_review_loop 覆盖，不受影响；``build_reflection_run_impl``
在未配置 client 时退化为确定性评审（不 hit 真实 API）。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from athena.agents.reflection_agent import (
    ReflectionAgent,
    build_reflection_run_impl,
)
from athena.core.agent.models import AgentContext, AgentOutcome
from athena.core.agent.provider import StreamEvent
from athena.core.agent.runtime import Agent
from athena.core.artifact_store import LocalArtifactStore
from athena.core.bundle import DirectoryBundle
from athena.core.tool import ToolRegistry
from athena.research.contracts import DatasetRoleReview, EDAReview


async def _noop_emit(*_a: object) -> None:
    return None


def _ctx(*, input_text: str = "{}") -> AgentContext:
    return AgentContext(
        thread=object(),
        turn=SimpleNamespace(turn_id="t1"),
        emit=_noop_emit,
        tools=object(),
        cancel=asyncio.Event(),
        input_text=input_text,
    )


class _FakeReviewProvider:
    """结构化评审 fake provider：直接 emit 合法 decision JSON。"""

    model_name = "fake"

    async def stream(self, config, tools, messages, cancel, *, output_type=None):
        review_json = json.dumps({"decision": "REVISE", "findings": ["x"]})
        yield StreamEvent(
            kind="text_delta", data={"delta": review_json, "accumulated": review_json}
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


@pytest.mark.asyncio
async def test_role_review_via_run_impl(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    async def fake_review(ctx: AgentContext) -> AgentOutcome:
        del ctx
        text = json.dumps(
            DatasetRoleReview(decision="ACCEPT").model_dump(), ensure_ascii=False
        )
        return AgentOutcome(result_ref=await store.put_text(text))

    agent = ReflectionAgent(store, run_impl=fake_review)
    outcome = await agent.run(_ctx())
    review = DatasetRoleReview.model_validate_json(
        await store.get_text(outcome.result_ref)
    )
    assert review.decision == "ACCEPT"


@pytest.mark.asyncio
async def test_eda_review_via_run_impl(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    async def fake_review(ctx: AgentContext) -> AgentOutcome:
        del ctx
        text = json.dumps(
            EDAReview(
                decision="REVISE", findings=["missing distribution"]
            ).model_dump(),
            ensure_ascii=False,
        )
        return AgentOutcome(result_ref=await store.put_text(text))

    agent = ReflectionAgent(store, run_impl=fake_review)
    outcome = await agent.run(_ctx())
    review = EDAReview.model_validate_json(await store.get_text(outcome.result_ref))
    assert review.decision == "REVISE"
    assert review.findings == ["missing distribution"]


@pytest.mark.asyncio
async def test_deterministic_role_review_reads_proposal_not_report(tmp_path) -> None:
    """两阶段合同：role 评审读 dataset_role_proposal.json，而不是 EDA report.md。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    source = await DirectoryBundle.commit(
        store,
        {
            "report.md": await store.put_text("EDA report only"),
            "dataset_role_proposal.json": await store.put_text(
                '{"role_proposal": "train", "data_files": ["train.csv"]}'
            ),
            "figures/plot.png": await store.put_text("png"),
        },
    )
    agent = ReflectionAgent(store)
    role = json.loads(
        await store.get_text(
            (await agent._review(source, report_only=False, kind="role")).result_ref
        )
    )
    assert role["decision"] == "ACCEPT"
    # 证据指向 proposal 而非 report
    assert "role_proposal" in await store.get_text(role["evidence_refs"][0])
    # 同一 bundle 的 EDA 评审读 report.md
    eda = json.loads(
        await store.get_text(
            (await agent._review(source, report_only=False, kind="eda")).result_ref
        )
    )
    assert eda["decision"] == "ACCEPT"


@pytest.mark.asyncio
async def test_deterministic_role_review_accepts_direct_proposal(tmp_path) -> None:
    """角色评审直接消费 DatasetRoleProposal Artifact。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    source = await store.put_text(
        json.dumps(
            {
                "role_proposal": "data.csv is training data",
                "data_files": ["data.csv"],
                "target_column": "label",
                "reasoning": "label is present only in the training data",
            }
        )
    )

    role = json.loads(
        await store.get_text(
            (
                await ReflectionAgent(store)._review(
                    source, report_only=False, kind="role"
                )
            ).result_ref
        )
    )

    assert role["decision"] == "ACCEPT"
    assert role["evidence_refs"] == [source]


@pytest.mark.asyncio
async def test_deterministic_role_review_revises_without_proposal(tmp_path) -> None:
    """两阶段合同：role 评审缺少 proposal JSON → REVISE（不能拿 EDA report 顶替）。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    source = await DirectoryBundle.commit(
        store,
        {
            "report.md": await store.put_text("EDA report"),
            "figures/plot.png": await store.put_text("png"),
        },
    )
    agent = ReflectionAgent(store)
    payload = json.loads(
        await store.get_text(
            (await agent._review(source, report_only=False, kind="role")).result_ref
        )
    )
    assert payload["decision"] == "REVISE"


@pytest.mark.asyncio
async def test_built_run_impl_without_client_falls_back_deterministic(tmp_path) -> None:
    """build_reflection_run_impl 未配置 client → 确定性评审（不 hit API）。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    source = await DirectoryBundle.commit(
        store,
        {
            "report.md": await store.put_text("分析报告"),
            "figures/plot.png": await store.put_text("png"),
        },
    )
    impl = build_reflection_run_impl(store, model="fake", client=None)
    outcome = await impl(
        _ctx(
            input_text=json.dumps(
                {"content": "review role proposal", "data_analysis_ref": source}
            )
        )
    )
    payload = json.loads(await store.get_text(outcome.result_ref))
    assert payload["decision"] == "ACCEPT"
    assert payload["evidence_refs"]


@pytest.mark.asyncio
async def test_built_run_impl_revises_on_empty_report(tmp_path) -> None:
    """确定性 fallback：报告为空 → REVISE（装配路径可用作无 client 时的安全评审）。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    source = await DirectoryBundle.commit(
        store,
        {"report.md": await store.put_text("")},
    )
    impl = build_reflection_run_impl(store, model="fake", client=None)
    outcome = await impl(
        _ctx(
            input_text=json.dumps(
                {"content": "review role proposal", "data_analysis_ref": source}
            )
        )
    )
    payload = json.loads(await store.get_text(outcome.result_ref))
    assert payload["decision"] == "REVISE"
    assert "report 为空或缺失" in payload["issues"]


@pytest.mark.asyncio
async def test_built_run_impl_selects_output_contract_by_kind(tmp_path) -> None:
    """真实 LLM 路径按请求 ``kind`` 选择 EDAReview / DatasetRoleReview 输出合同。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    source = await DirectoryBundle.commit(
        store, {"report.md": await store.put_text("分析报告")}
    )
    seen: dict[str, object] = {}

    def builder(*, model, client, output_type, artifacts, **kwargs):
        del model, client, artifacts, kwargs
        seen["output_type"] = output_type
        return Agent(
            _FakeReviewProvider(),
            ToolRegistry(),
            "review",
            output_type=output_type,
            artifacts=store,
        )

    impl = build_reflection_run_impl(
        store, model="fake", client="fake-client", agent_builder=builder
    )
    # kind=eda → EDAReview
    outcome = await impl(
        _ctx(input_text=json.dumps({"kind": "eda", "data_analysis_ref": source}))
    )
    assert seen["output_type"] is EDAReview
    review = EDAReview.model_validate_json(await store.get_text(outcome.result_ref))
    assert review.decision == "REVISE"
    # kind=role → DatasetRoleReview
    outcome = await impl(
        _ctx(input_text=json.dumps({"kind": "role", "data_analysis_ref": source}))
    )
    assert seen["output_type"] is DatasetRoleReview
    review = DatasetRoleReview.model_validate_json(
        await store.get_text(outcome.result_ref)
    )
    assert review.decision == "REVISE"


async def test_built_run_impl_with_runtime_injects_summary_and_shell(
    tmp_path, monkeypatch
) -> None:
    """runtime 提供时 reflection 内层 prompt 注入运行时摘要，工具含 shell_command。

    只读评审不放 read/write/bash；shell_command 以 project_root 为 workspace。
    """
    from athena.execution.runtime import ExecutionRuntime

    store = LocalArtifactStore(tmp_path / "artifacts")
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, provider, tools, prompt, **kwargs):
            del provider, kwargs
            captured["prompt"] = prompt
            captured["tools"] = tools
            self.tools = tools

        async def run(self, ctx):
            del ctx
            return AgentOutcome(result_ref="review-ref")

    monkeypatch.setattr("athena.agents.reflection_agent.Agent", FakeAgent)
    runtime = ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path)
    impl = build_reflection_run_impl(
        store, model="fake", client=object(), runtime=runtime
    )
    outcome = await impl(_ctx())
    assert str(captured["prompt"]).startswith("Runtime:")
    names = {s.name for s in captured["tools"].specs}
    assert "shell_command" in names
    assert "write_file" not in names  # 只读评审不放写工具
    assert outcome.result_ref == "review-ref"
