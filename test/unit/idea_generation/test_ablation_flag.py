"""消融开关：两套 ideation 必须能各自完整跑，而不是新的把旧的替换掉。

``gated``（本次迁移的 Idea Generation：结构/可证伪性门禁 + 多视角审阅 + 排序）与
``baseline``（main 原有行为：Ideator 产出即入库）走不同的输出契约和不同的 prompt，
所以开关必须一路传到 agent 注册处，不能只在末尾加个 if。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents.ideator_agent import register_ideator_agent
from athena.core.research_models import Hypothesis, HypothesisBatch
from athena.research.agent_turn_runner import AgentTurnRunner
from athena.research.idea_generation.idea_schemas import (
    IdeatorHypothesisBatch,
    IdeatorHypothesisDraft,
)

_PROMPT_DIR = Path("src/athena/agents/prompts")


class _Registry:
    """最小 AgentTypeRegistry 替身：只记下注册进来的工厂。"""

    def __init__(self) -> None:
        self.factories: dict[str, object] = {}

    def register(self, agent_type: str, factory) -> None:
        self.factories[agent_type] = factory

    def contains(self, agent_type: str) -> bool:
        return agent_type in self.factories


def _execution_runtime() -> SimpleNamespace:
    """ExecutionRuntime 替身：generic_tool_registry 只用到 shell_command_tool。"""
    return SimpleNamespace(
        shell_command_tool=lambda _root: _noop_tool(),
        runtime_summary=lambda _w: "",
    )


def _noop_tool():
    from athena.core.tool import tool

    @tool
    async def shell_command(command: str) -> dict:
        """Stub shell tool; never invoked in these tests."""
        return {"stdout": ""}

    return shell_command


def _register(gated: bool | None, tmp_path: Path):
    registry = _Registry()
    kwargs = {} if gated is None else {"gated": gated}
    register_ideator_agent(
        registry,
        provider=object(),
        artifacts=SimpleNamespace(),
        workspace=tmp_path,
        runtime=_execution_runtime(),
        **kwargs,
    )
    return registry.factories["ideator"]("agent-1")


def _output_type_of(gated: bool, tmp_path: Path):
    """注册一次并取出内层 Agent 绑定的结构化输出契约。"""
    return _register(gated, tmp_path).runner._agent._output_type


# ====== 输出契约随开关切换 ======


def test_gated_mode_binds_the_rich_output_contract(tmp_path):
    assert _output_type_of(True, tmp_path) is IdeatorHypothesisBatch


def test_baseline_mode_binds_mains_original_output_contract(tmp_path):
    """baseline 必须真的用回 main 原来的 HypothesisBatch，否则不是同一个对照组。"""
    assert _output_type_of(False, tmp_path) is HypothesisBatch


def test_gated_is_the_default(tmp_path):
    """本次交付的能力默认开启；消融时显式关掉。"""
    assert (
        _register(None, tmp_path).runner._agent._output_type is IdeatorHypothesisBatch
    )


# ====== 两份 prompt 各自存在且要求不同 ======


def test_baseline_prompt_does_not_demand_the_rich_fields():
    """baseline prompt 不能要求 premises/disconfirmers——那是 gated 侧的契约。

    否则 baseline 组被迫按新格式作答，对照就失去意义。
    """
    text = (_PROMPT_DIR / "ideator_agent.md").read_text(encoding="utf-8")
    assert "disconfirming_observations" not in text
    assert "supported_premises" not in text


def test_gated_prompt_demands_the_rich_fields():
    text = (_PROMPT_DIR / "ideator_gated_agent.md").read_text(encoding="utf-8")
    assert "disconfirming_observations" in text
    assert "supported_premises" in text


# ====== 出口分支：门禁只在 gated 侧跑 ======


async def _no_corpus() -> set[str]:
    """没开文献调研的运行：引用校验无从比对，应原样放行。"""
    return set()


def _runner(ideation: str) -> AgentTurnRunner:
    """AgentTurnRunner 只需要 runtime 上的这几个字段就能做出口分支。"""
    return AgentTurnRunner(
        SimpleNamespace(
            model="fake-model",
            store=SimpleNamespace(),
            ideation=ideation,
            corpus_paper_ids=_no_corpus,
        )
    )


@pytest.mark.asyncio
async def test_baseline_mode_registers_ideator_output_untouched(monkeypatch):
    """baseline：Ideator 产出直接入库，一次门禁调用都不该发生。"""
    called = {"gate": 0}

    async def _tripwire(drafts, *, model, artifacts, **kwargs):
        called["gate"] += 1
        return []

    monkeypatch.setattr(
        "athena.research.agent_turn_runner.run_light_pipeline", _tripwire
    )
    produced = [Hypothesis(statement="s", intervention="i", expected_effect="e")]

    result = await _runner("baseline")._finish_ideator_batch(
        HypothesisBatch(hypotheses=produced)
    )

    assert called["gate"] == 0
    assert result.hypotheses == produced


@pytest.mark.asyncio
async def test_ideageneration_mode_sends_drafts_through_the_gate(monkeypatch):
    kept = [Hypothesis(statement="kept", intervention="i", expected_effect="e")]
    called = {"gate": 0}

    async def _fake_gate(drafts, *, model, artifacts, **kwargs):
        called["gate"] += 1
        return kept

    monkeypatch.setattr(
        "athena.research.agent_turn_runner.run_light_pipeline", _fake_gate
    )
    draft = IdeatorHypothesisDraft(
        statement="s",
        intervention="i",
        expected_effect="e",
        supported_premises=[],
        predicted_observations=["p"],
        disconfirming_observations=["d"],
    )

    result = await _runner("ideageneration")._finish_ideator_batch(
        IdeatorHypothesisBatch(hypotheses=[draft])
    )

    assert called["gate"] == 1
    assert result.hypotheses == kept
