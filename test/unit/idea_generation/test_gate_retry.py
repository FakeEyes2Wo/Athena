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
from athena.research import agent_turn_runner as atr
from athena.research.agent_turn_runner import MAX_GATE_RETRIES, AgentTurnRunner


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
        _model="m",
        _store=SimpleNamespace(),
        _ideation="ideageneration",
        _supervisor=SimpleNamespace(evaluator_ref=None),
        _events_bus=SimpleNamespace(project_agent_event=lambda *a, **k: None),
        publish_output=_noop_publish,
        survey_corpus_ref=lambda: None,
    )
    return AgentTurnRunner(runtime)


async def _noop_publish(**_kwargs) -> None:
    pass


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

    monkeypatch.setattr(atr, "wait_run_events", _wait)
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

    kept = await _runner(agents, tmp_path)._run_ideator_lane("ideator-1", 1, tmp_path)

    assert len(kept.hypotheses) == 1
    assert len(agents.followups) == 1
    assert "risk_ok_methodology" in agents.followups[0]


@pytest.mark.asyncio
async def test_retries_are_capped(tmp_path, monkeypatch):
    """门禁一直拒也必须停下来，不能无限重生成。"""
    agents = _Agents()
    calls = _patch_turn(monkeypatch, [])

    kept = await _runner(agents, tmp_path)._run_ideator_lane("ideator-1", 1, tmp_path)

    assert kept.hypotheses == []
    assert len(agents.followups) == MAX_GATE_RETRIES
    assert calls["n"] == MAX_GATE_RETRIES + 1


@pytest.mark.asyncio
async def test_first_round_success_does_not_retry(tmp_path, monkeypatch):
    agents = _Agents()
    _patch_turn(monkeypatch, [[_hyp()]])

    kept = await _runner(agents, tmp_path)._run_ideator_lane("ideator-1", 1, tmp_path)

    assert len(kept.hypotheses) == 1
    assert agents.followups == []


@pytest.mark.asyncio
async def test_baseline_mode_never_retries(tmp_path, monkeypatch):
    """对照组不过门禁，没有"全拒"这回事，不能引入 gated 才有的重试差异。"""
    agents = _Agents()
    runner = _runner(agents, tmp_path)
    runner._runtime._ideation = "baseline"
    _patch_turn(monkeypatch, [[]])

    kept = await runner._run_ideator_lane("ideator-1", 1, tmp_path)

    assert kept.hypotheses == []
    assert agents.followups == []
