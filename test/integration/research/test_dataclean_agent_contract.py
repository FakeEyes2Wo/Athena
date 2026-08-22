"""run_dataclean_plan 修复循环 + 数据无关完成 gate 的契约测试。"""

import json
from pathlib import Path

import pytest

from athena.agents.prepare_dataclean_agent import register_dataclean_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.artifact_store import LocalArtifactStore
from athena.execution.runtime import ExecutionRuntime
from athena.research.supervisor.prepare import run_dataclean_plan

HANDOFF_COCO = (
    "# DataClean Handoff\n"
    "Inspected the COCO annotations: 2 images, 1 polygon; no issues, data clean.\n"
)
HANDOFF_TABULAR = (
    "# DataClean Handoff\n"
    "Inspected train.csv: 20 rows, 5 cols; filled 2 missing cells, deduped 1 row.\n"
)


def _submit() -> str:
    return json.dumps({"decision": "submit", "reason": "clean", "suggestions": []})


def _abandon() -> str:
    return json.dumps({"decision": "abandon", "reason": "no data", "suggestions": []})


class _DataCleanProvider:
    """动作队列 provider：每 stream 一个 write_file / submit / abandon。

    action 取值：``None`` → submit 答案；``"abandon"`` → abandon 答案；否则
    ``(path, content)`` → write_file。沿用 ``_EvaluatorProvider`` 语义：一次 plan
    迭代内部会跑多轮 stream（write_file 执行后继续），直到 provider 交出文本答案。
    """

    model_name = "dataclean-integration"

    def __init__(self) -> None:
        self.calls = 0
        self._actions: list[tuple[str, str] | None | str] = []

    def set_actions(self, actions: list[tuple[str, str] | None | str]) -> None:
        self._actions = list(actions)

    async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
        self.calls += 1
        action = self._actions.pop(0)
        if action is None:
            answer = _submit()
        elif action == "abandon":
            answer = _abandon()
        else:
            path, content = action
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": f"write-{self.calls}",
                    "name": "write_file",
                    "arguments": {"path": path, "content": content},
                },
            )
            yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})
            return
        yield StreamEvent(
            kind="text_delta", data={"delta": answer, "accumulated": answer}
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.store = LocalArtifactStore(tmp_path / "artifacts")
        self.dataclean_dir = tmp_path / "dataclean"
        self.runtime = ExecutionRuntime(
            project_root=self.dataclean_dir, store=self.store
        )
        self.provider = _DataCleanProvider()

    async def run(self, *, max_turns: int = 4, task: str = "clean the data") -> str:
        registry = AgentTypeRegistry()
        register_dataclean_agent(
            registry,
            provider=self.provider,
            artifacts=self.store,
            workspace=self.dataclean_dir,
            runtime=self.runtime,
        )
        agents = AgentRuntime(
            type_registry=registry,
            project_root=self.tmp_path,
            rollout_dir=self.tmp_path / ".athena" / "logs" / "agents",
        )
        agents.start()
        try:
            return await run_dataclean_plan(
                agents=agents,
                store=self.store,
                dataclean_dir=self.dataclean_dir,
                execution=self.runtime,
                task=task,
                max_turns=max_turns,
            )
        finally:
            await agents.aclose()


@pytest.mark.asyncio
async def test_dataclean_plan_submits_with_handoff(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.provider.set_actions(
        [("clean.py", "print('cleaned')\n"), ("DATACLEAN_HANDOFF.md", HANDOFF_COCO), None]
    )
    text = await harness.run()
    assert text == HANDOFF_COCO
    handoff = harness.dataclean_dir / "DATACLEAN_HANDOFF.md"
    assert handoff.read_text(encoding="utf-8") == HANDOFF_COCO


@pytest.mark.asyncio
async def test_dataclean_plan_repairs_missing_handoff(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    # 首轮只写 clean.py 就 submit → gate 打回 → 补 handoff → 再 submit
    harness.provider.set_actions(
        [
            ("clean.py", "print('cleaned')\n"),
            None,
            ("DATACLEAN_HANDOFF.md", HANDOFF_COCO),
            None,
        ]
    )
    text = await harness.run()
    assert text == HANDOFF_COCO
    # 修复轮确实发生：agent 提交了两次
    assert harness.provider.calls == 4


@pytest.mark.asyncio
async def test_dataclean_plan_abandon_raises(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.provider.set_actions(["abandon"])
    with pytest.raises(RuntimeError, match="abandoned"):
        await harness.run()


@pytest.mark.asyncio
async def test_dataclean_plan_budget_exhausted(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    # 一直不写 handoff，max_turns=1 → budget 耗尽
    harness.provider.set_actions([("clean.py", "print('cleaned')\n"), None])
    with pytest.raises(RuntimeError, match="turn budget exhausted"):
        await harness.run(max_turns=1)


@pytest.mark.parametrize("handoff", [HANDOFF_COCO, HANDOFF_TABULAR])
@pytest.mark.asyncio
async def test_dataclean_gate_is_dataset_agnostic(
    tmp_path: Path, handoff: str
) -> None:
    # 两种形状完全不同（COCO 标注 vs 纯表格），gate 只认“非空 handoff + submit”
    harness = _Harness(tmp_path)
    harness.provider.set_actions(
        [("clean.py", "print('cleaned')\n"), ("DATACLEAN_HANDOFF.md", handoff), None]
    )
    text = await harness.run(task=f"clean {handoff.splitlines()[0]}")
    assert text == handoff
