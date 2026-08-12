"""production run_impl 测试：真实 Ideator/Code 经 FakeStreamingClient 驱动（plan Task 3 Step 1）。

验证 run_impl 真的调用模型 provider（session 有采样），且产出不含 fallback 标记
（fallback-change / cand_fallback / labels / test_score）。
"""

import asyncio
import json
from pathlib import Path

import pytest

from athena.agents.production import code_run_impl, structured_ideator_run_impl
from athena.core.agent.models import AgentContext
from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_models import HypothesisBatch
from athena.core.thread_models import AthenaThread, AthenaTurn
from athena.core.tool import ToolRegistry
from athena.memory.context_manager import ContextManager

from test.unit._support import FakeStreamingClient


async def _noop_emit(*_a: object) -> None:
    return None


def _ctx(input_text: str) -> AgentContext:
    return AgentContext(
        thread=AthenaThread(
            thread_id="t1", session_id="s1", status="running", context_ref="c1"
        ),
        turn=AthenaTurn(
            turn_id="t1-turn", thread_id="t1", request_ref="c1", status="running"
        ),
        emit=_noop_emit,
        cancel=asyncio.Event(),
        tools=ToolRegistry(),
        memory=ContextManager(),
        input_text=input_text,
    )


async def test_structured_ideator_uses_provider_not_fallback(tmp_path: Path) -> None:
    """ideator run_impl 调用 provider，产出 HypothesisBatch，不含 fallback-change。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    client = FakeStreamingClient()
    impl = structured_ideator_run_impl(store, model="fake", client=client)
    outcome = await impl(_ctx("{}"))
    assert client.calls >= 1  # 真实调用 provider
    payload = json.loads(await store.get_text(outcome.result_ref))
    batch = HypothesisBatch.model_validate(payload)
    assert batch.hypotheses
    assert "fallback-change" not in json.dumps(payload)


async def test_code_impl_writes_required_files_and_no_labels(tmp_path: Path) -> None:
    """code run_impl 在分配工作区产出 model.py/predictions.csv/REPORT.md，不含 labels/score。"""
    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "work"
    workspace.mkdir(parents=True, exist_ok=True)
    client = FakeStreamingClient()
    impl = code_run_impl(store, model="fake", client=client, project_root=tmp_path)
    assignment = {
        "experiment_id": "exp_1",
        "workspace": str(workspace),
        "hypothesis": "scale features",
        "environment_root": str(workspace),
    }
    outcome = await impl(_ctx(json.dumps(assignment)))
    assert client.calls >= 2  # 首轮工具调用 + 后续完成
    for name in ("model.py", "predictions.csv", "REPORT.md"):
        assert (workspace / name).is_file()
    result = json.loads(await store.get_text(outcome.result_ref))
    assert result["experiment_id"] == "exp_1"
    text = json.dumps(result)
    assert "labels" not in text
    assert "test_score" not in text
    assert "cand_fallback" not in text


@pytest.mark.asyncio
async def test_code_impl_repairs_missing_required_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athena.agents import production
    from athena.core.agent.models import AgentOutcome

    store = LocalArtifactStore(tmp_path / "artifacts")
    workspace = tmp_path / "work"
    workspace.mkdir()

    class RepairingInner:
        def __init__(self) -> None:
            self.tools = ToolRegistry()
            self.prompts: list[str] = []

        async def run(self, ctx: AgentContext) -> AgentOutcome:
            self.prompts.append(ctx.input_text or "")
            (workspace / "model.py").write_text("print('model')\n", encoding="utf-8")
            (workspace / "predictions.csv").write_text(
                "__athena_row_id,prediction\n1,0\n", encoding="utf-8"
            )
            if len(self.prompts) > 1:
                (workspace / "REPORT.md").write_text("# Report\n", encoding="utf-8")
            return AgentOutcome(result_ref="inner://done")

    inner = RepairingInner()
    monkeypatch.setattr(production, "build_llm_agent", lambda *_a, **_k: inner)
    impl = code_run_impl(store, model="fake", client=None, project_root=tmp_path)

    outcome = await impl(
        _ctx(json.dumps({"experiment_id": "exp_1", "workspace": str(workspace)}))
    )

    assert outcome.result_ref.startswith("sha256:")
    assert len(inner.prompts) == 2
    assert "REPORT.md" in inner.prompts[1]
