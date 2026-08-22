"""run_prepare_phase 中 dataclean 步骤位于 evaluator 之后、EDA 之前的排序测试。"""

import json
from pathlib import Path

import pytest

from test.unit._support import FakeProvider

from athena.core.agent import settings
from athena.research.phase_runner import PhaseRunner
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.prepare import PrepareResult


def _ref(hexchar: str) -> str:
    return "sha256:" + hexchar * 64


@pytest.mark.asyncio
async def test_dataclean_step_sits_between_evaluator_and_eda(
    tmp_path: Path, monkeypatch
) -> None:
    order: list[str] = []

    async def stub_run_evaluator_plan(**kwargs) -> str:
        order.append("evaluator")
        return await kwargs["store"].put_text(
            json.dumps({"bundle_id": "evaluator", "entrypoint": "evaluate.py"})
        )

    async def stub_run_dataclean_plan(**kwargs) -> str:
        order.append("dataclean")
        return "cleaned handoff text"

    async def stub_run_prepare_plan(**kwargs) -> PrepareResult:
        order.append("prepare")
        return PrepareResult(
            evaluator_ref=_ref("a"),
            metric=0.7,
            commit="deadbeef",
            predictions_ref=_ref("b"),
            evidence_ref=_ref("c"),
            report_ref=_ref("d"),
        )

    monkeypatch.setattr(
        "athena.research.phase_runner.run_evaluator_plan", stub_run_evaluator_plan
    )
    monkeypatch.setattr(
        "athena.research.phase_runner.run_dataclean_plan", stub_run_dataclean_plan
    )
    monkeypatch.setattr(
        "athena.research.phase_runner.run_prepare_plan", stub_run_prepare_plan
    )

    async def stub_handoff(
        self, *, agent_id, agent_type, workspace, output_file, content
    ):
        order.append(agent_id)
        path = Path(workspace) / output_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
        return "ok"

    monkeypatch.setattr(PhaseRunner, "_run_handoff_agent", stub_handoff)

    async def stub_run_eda_todos(**kwargs):
        order.append("eda_todos")
        return []

    monkeypatch.setattr(
        "athena.research.phase_runner.run_eda_todos", stub_run_eda_todos
    )

    runtime = ResearchRuntime(
        project_root=tmp_path,
        task="predict survival",
        model=settings.model_name(),
        client=FakeProvider(),
    )
    try:
        await runtime._phase_runner.run_prepare_phase()
    finally:
        await runtime.aclose()

    assert order == [
        "evaluator",
        "dataclean",
        "prepare_eda",  # EDA_TODO
        "eda_todos",
        "prepare_eda",  # EDA finalize
        "baseline_ideator",
        "prepare",
    ]
