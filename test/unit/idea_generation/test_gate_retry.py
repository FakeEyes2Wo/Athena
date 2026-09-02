"""门禁全拒时带反馈重新生成，且必须有硬性上限。

真实跑测（MazeCrawler，2026-08-15）暴露：门禁把唯一候选 REJECT 后，
run_ideator_turn 返回空列表 -> _fill_slots 的 generated=False -> run_search 直接
return -> 状态停在 RUNNING 既不推进也不终止，整个 loop 静默死掉。

"门禁拒了"本身是给生成侧的有效信号，所以重新生成；但无上限重试会在门禁持续拒绝时
变成死循环，故设上限。
"""

from types import SimpleNamespace

import pytest

from athena.core.research_models import Hypothesis, HypothesisBatch
from athena.research.turns import ideator as atr
from athena.research.turns.ideator import MAX_GATE_RETRIES
from athena.research.turns.runner import AgentTurnRunner


class _Agents:
    """AgentRuntime 替身：记录 create_root 与 followup 的调用。"""

    def __init__(self) -> None:
        self.followups: list[str] = []
        self.created = 0

    async def create_root(self, agent_type, request, *, name=None):
        self.created += 1
        return "agent-1", f"run-{self.created}"

    async def followup(self, agent_id, request):
        self.followups.append(request["content"])
        return f"run-followup-{len(self.followups)}"


def _runner(agents: _Agents, tmp_path) -> AgentTurnRunner:
    runtime = SimpleNamespace(
        _agents=agents,
        agents=agents,
        _model="m",
        model="m",
        client=None,
        _store=SimpleNamespace(),
        store=SimpleNamespace(),
        _ideation="ideageneration",
        ideation="ideageneration",
        _supervisor=SimpleNamespace(evaluator_ref=None),
        supervisor=SimpleNamespace(evaluator_ref=None),
        _events_bus=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        publish_output=_noop_publish,
        survey_corpus_ref=lambda: None,
        start_corpus_round=lambda: None,
        corpus_papers_read=set,
        corpus_paper_ids=_no_corpus,
        state=SimpleNamespace(
            handoff_sources=[],
            handoff_refs={},
            ideator_count=3,
            hypotheses_per_ideator=2,
        ),
    )
    return AgentTurnRunner(runtime)


async def _noop_publish(**_kwargs) -> None:
    pass


async def _no_corpus() -> set[str]:
    """没开文献调研的运行：引用校验无从比对，应原样放行。"""
    return set()


def _patch_turn(monkeypatch, kept_per_attempt):
    """让每次 turn 依次返回 kept_per_attempt 里的结果；rejections 恒非空。"""
    calls = {"n": 0}

    async def _wait(*_a, **_k):
        return SimpleNamespace(error=None)

    async def _load(_summary, _store, _schema):
        return HypothesisBatch(hypotheses=[])

    async def _gate(drafts, *, model, artifacts, rejections=None, **kwargs):
        index = calls["n"]
        calls["n"] += 1
        kept = kept_per_attempt[index] if index < len(kept_per_attempt) else []
        if not kept and rejections is not None:
            rejections.append(f"idea-{index}: risk_ok_methodology")
        return kept

    monkeypatch.setattr("athena.research.turns.common.wait_run_events", _wait)
    monkeypatch.setattr(atr, "load_agent_result", _load)
    monkeypatch.setattr(atr, "run_light_pipeline", _gate)
    return calls


def _hyp() -> Hypothesis:
    return Hypothesis(statement="s", intervention="i", expected_effect="e")


@pytest.mark.asyncio
async def test_all_rejected_triggers_a_followup_with_the_blocking_reasons(
    tmp_path, monkeypatch
):
    """第一轮全拒 -> 带 blocking factor 反馈重新生成 -> 第二轮通过。"""
    agents = _Agents()
    _patch_turn(monkeypatch, [[], [_hyp()]])

    kept = await _runner(agents, tmp_path)._run_ideator_lane("ideator-1-1", 1, tmp_path)

    assert len(kept.hypotheses) == 1
    assert len(agents.followups) == 1
    assert "risk_ok_methodology" in agents.followups[0]


@pytest.mark.asyncio
async def test_retries_are_capped(tmp_path, monkeypatch):
    """门禁一直拒也必须停下来，不能无限重生成。"""
    agents = _Agents()
    calls = _patch_turn(monkeypatch, [])

    kept = await _runner(agents, tmp_path)._run_ideator_lane("ideator-1-1", 1, tmp_path)

    assert kept.hypotheses == []
    assert len(agents.followups) == MAX_GATE_RETRIES
    assert calls["n"] == MAX_GATE_RETRIES + 1


@pytest.mark.asyncio
async def test_first_round_success_does_not_retry(tmp_path, monkeypatch):
    agents = _Agents()
    _patch_turn(monkeypatch, [[_hyp()]])

    kept = await _runner(agents, tmp_path)._run_ideator_lane("ideator-1-1", 1, tmp_path)

    assert len(kept.hypotheses) == 1
    assert agents.followups == []


@pytest.mark.asyncio
async def test_baseline_mode_never_retries(tmp_path, monkeypatch):
    """对照组不过门禁，没有"全拒"这回事，不能引入 gated 才有的重试差异。"""
    agents = _Agents()
    runner = _runner(agents, tmp_path)
    runner._runtime._ideation = "baseline"
    runner._runtime.ideation = "baseline"
    _patch_turn(monkeypatch, [[]])

    kept = await runner._run_ideator_lane("ideator-1-1", 1, tmp_path)

    assert kept.hypotheses == []
    assert agents.followups == []


@pytest.mark.asyncio
async def test_a_lane_that_succeeds_survives_the_trip_back_to_run_ideator_turn(
    tmp_path, monkeypatch
):
    """真实跑测（2026-08-16）：SEARCH 死在 ``'list' object has no attribute 'hypotheses'``。

    ``_run_ideator_lane`` 标注返回 ``HypothesisBatch``，实际每条 return 都给的是
    ``list``，而 ``run_ideator_turn`` 按 ``result.hypotheses`` 取值——于是任何一条
    **成功**的 lane 都会让整个 SEARCH 崩掉。上面几个用例只测到 lane 自身，看不到这
    一步，所以这里直接跑 ``run_ideator_turn`` 把两侧接起来。
    """
    agents = _Agents()
    _patch_turn(monkeypatch, [[_hyp()], [_hyp()]])
    runner = _runner(agents, tmp_path)
    data_turns: list[str] = []

    async def _record_data_turn(request: str) -> None:
        data_turns.append(request)

    async def _publish_ideator_state() -> None:
        pass

    runner.run_data_turn = _record_data_turn
    runner._runtime._provider = object()
    runner._runtime.provider = object()
    runner._runtime._registry = SimpleNamespace(contains=lambda _name: True)
    runner._runtime.registry = SimpleNamespace(contains=lambda _name: True)
    runner._runtime.state = SimpleNamespace(
        ideator_count=1,
        hypotheses_per_ideator=1,
        handoff_sources=[],
        handoff_refs={},
    )
    runner._runtime._events_bus.set_ideator_lanes = lambda _count: None
    runner._runtime._events_bus.publish_ideator_state = _publish_ideator_state
    runner._runtime.events = runner._runtime._events_bus
    runner._resolve_eda_dir = lambda _rt: str(tmp_path)

    hypotheses = await runner.run_ideator_turn(1)

    assert [type(item).__name__ for item in hypotheses] == ["Hypothesis"]
    assert data_turns == []
