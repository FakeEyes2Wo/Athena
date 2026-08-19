"""Focused tests for concurrent Ideator fan-out and display projection."""

import asyncio
from dataclasses import dataclass
from types import MethodType, SimpleNamespace

import pytest

from athena.core.research_models import Hypothesis, HypothesisBatch
from athena.research.agent_turn_runner import AgentTurnRunner
from athena.research.idea_generation.idea_schemas import (
    IdeatorHypothesisBatch,
    IdeatorHypothesisDraft,
)
from athena.research.runtime import ResearchRuntime


def _hypothesis(label: str) -> Hypothesis:
    return Hypothesis(
        statement=f"statement {label}",
        intervention=f"change {label}",
        expected_effect="improve metric",
    )


@pytest.mark.parametrize(
    ("count", "lanes", "expected"),
    [
        (1, 3, (1,)),
        (2, 3, (1, 1)),
        (4, 3, (2, 1, 1)),
        (6, 3, (2, 2, 2)),
        (8, 3, (3, 3, 2)),
        (6, 2, (3, 3)),
    ],
)
def test_ideator_allocations_cap_workers_at_lanes(count, lanes, expected) -> None:
    assert AgentTurnRunner._ideator_allocations(count, lanes) == expected


@pytest.mark.asyncio
async def test_ideator_turn_runs_actual_lane_count_concurrently_and_merges_in_order(
    tmp_path,
) -> None:
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._corpus_sessions = []
    runtime._provider = object()
    runtime._state = SimpleNamespace(
        eda_dir=str(tmp_path), ideator_count=3, hypotheses_per_ideator=2
    )
    runtime._supervisor = SimpleNamespace(state=runtime._state)
    runtime._registry = SimpleNamespace(contains=lambda _name: True)
    started: list[tuple[str, int]] = []
    all_started = asyncio.Event()

    async def run_lane(
        self, label: str, target: int, _eda_dir, profile=None, handoff_texts=None
    ):
        started.append((label, target))
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        return HypothesisBatch(hypotheses=[_hypothesis(label)] * target)

    runner = AgentTurnRunner(runtime)
    runner._run_ideator_lane = MethodType(run_lane, runner)

    hypotheses = await runner.run_ideator_turn(4)

    assert started == [
        ("ideator-1-1", 2),
        ("ideator-1-2", 2),
        ("ideator-1-3", 2),
    ]
    assert [item.intervention for item in hypotheses] == [
        "change ideator-1-1",
        "change ideator-1-1",
        "change ideator-1-2",
        "change ideator-1-2",
        "change ideator-1-3",
        "change ideator-1-3",
    ]


@dataclass(frozen=True)
class _Event:
    kind: str
    event_ref: str
    data: dict | None = None


class _EventRuntime:
    def run_events(self, _run_id, *, after_sequence=0):
        assert after_sequence == 0

        async def events():
            yield _Event("agent/text_delta", "event:1", {"delta": "idea"})
            yield _Event("turn_completed", "event:2")

        return events()

    async def wait_run(self, _run_id):
        return SimpleNamespace(response_ref=None, status=None, error=None)


@pytest.mark.asyncio
async def test_ideator_event_forwarding_uses_stable_lane_label() -> None:
    from athena.research.supervisor.plans import wait_run_events

    events = _EventRuntime()
    projected: list[tuple[str, str, str, dict | None]] = []
    label = "ideator-2"

    async def publish(kind, event_ref, data):
        projected.append((label, kind, event_ref, data))

    await wait_run_events(events, "run-1", publish)

    assert projected == [
        ("ideator-2", "agent/text_delta", "event:1", {"delta": "idea"}),
        ("ideator-2", "turn_completed", "event:2", None),
    ]


@pytest.mark.asyncio
async def test_ideator_turn_keeps_successful_peers_when_one_lane_fails(
    tmp_path,
) -> None:
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._corpus_sessions = []
    runtime._provider = object()
    runtime._state = SimpleNamespace(
        eda_dir=str(tmp_path), ideator_count=3, hypotheses_per_ideator=2
    )
    runtime._supervisor = SimpleNamespace(state=runtime._state)
    runtime._registry = SimpleNamespace(contains=lambda _name: True)
    errors: list[tuple[str, str]] = []

    async def run_lane(
        self, label: str, _target: int, _eda_dir, profile=None, handoff_texts=None
    ):
        if label == "ideator-1-2":
            raise RuntimeError("offline")
        return HypothesisBatch(hypotheses=[_hypothesis(label)])

    async def publish_output(*, source, channel, text, plan=None, **_kwargs):
        assert source == "agent"
        assert channel == "error"
        errors.append((plan, text))

    runner = AgentTurnRunner(runtime)
    runner._run_ideator_lane = MethodType(run_lane, runner)
    runtime.publish_output = publish_output

    hypotheses = await runner.run_ideator_turn(3)

    assert [item.intervention for item in hypotheses] == [
        "change ideator-1-1",
        "change ideator-1-3",
    ]
    assert errors == [("ideator-1-2", "Ideator 2 failed: offline")]


@pytest.mark.asyncio
async def test_ideator_turn_keeps_every_generated_hypothesis_without_truncation(
    tmp_path,
) -> None:
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._corpus_sessions = []
    runtime._provider = object()
    runtime._state = SimpleNamespace(
        eda_dir=str(tmp_path), ideator_count=3, hypotheses_per_ideator=2
    )
    runtime._supervisor = SimpleNamespace(state=runtime._state)
    runtime._registry = SimpleNamespace(contains=lambda _name: True)

    async def run_lane(
        self, label: str, _target: int, _eda_dir, profile=None, handoff_texts=None
    ):
        return HypothesisBatch(
            hypotheses=[_hypothesis(f"{label}-a"), _hypothesis(f"{label}-b")]
        )

    runner = AgentTurnRunner(runtime)
    runner._run_ideator_lane = MethodType(run_lane, runner)

    hypotheses = await runner.run_ideator_turn(1)

    # 不按 count 截断：每条 lane 的全部产物都保留并进入 graph。
    assert len(hypotheses) == 6
    assert {item.intervention for item in hypotheses} == {
        "change ideator-1-1-a",
        "change ideator-1-1-b",
        "change ideator-1-2-a",
        "change ideator-1-2-b",
        "change ideator-1-3-a",
        "change ideator-1-3-b",
    }


@pytest.mark.asyncio
async def test_ideator_turn_resolves_relative_eda_dir_against_project_root(
    tmp_path,
) -> None:
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._corpus_sessions = []
    runtime._provider = object()
    runtime._root = tmp_path
    workspace = tmp_path / "workspaces" / "eda"
    workspace.mkdir(parents=True)
    runtime._state = SimpleNamespace(
        eda_dir="workspaces/eda", ideator_count=3, hypotheses_per_ideator=2
    )
    runtime._supervisor = SimpleNamespace(state=runtime._state)
    runtime._registry = SimpleNamespace(contains=lambda _name: True)
    resolved: list[str] = []

    async def run_lane(
        self, label: str, _target: int, eda_dir, profile=None, handoff_texts=None
    ):
        resolved.append(str(eda_dir))
        return HypothesisBatch(hypotheses=[_hypothesis(label)])

    runner = AgentTurnRunner(runtime)
    runner._run_ideator_lane = MethodType(run_lane, runner)

    hypotheses = await runner.run_ideator_turn(1)

    assert resolved == [str(workspace.resolve())] * 3
    assert [item.intervention for item in hypotheses] == [
        "change ideator-1-1",
        "change ideator-1-2",
        "change ideator-1-3",
    ]


@pytest.mark.asyncio
async def test_ideator_eda_request_dispatches_data_agent(tmp_path) -> None:
    """任一 Ideator lane 请求补充 EDA 时，触发 Data Agent 写回 EDA 目录。"""
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._corpus_sessions = []
    runtime._provider = object()
    runtime._root = tmp_path
    runtime._state = SimpleNamespace(
        eda_dir=str(tmp_path), ideator_count=2, hypotheses_per_ideator=1
    )
    runtime._supervisor = SimpleNamespace(state=runtime._state)
    runtime._registry = SimpleNamespace(contains=lambda _name: True)
    requests: list[str] = []

    async def run_lane(
        self, label: str, _target: int, _eda_dir, profile=None, handoff_texts=None
    ):
        if label == "ideator-1-1":
            return HypothesisBatch(
                hypotheses=[_hypothesis(label)],
                eda_request="correlation between age and target",
            )
        return HypothesisBatch(hypotheses=[_hypothesis(label)])

    async def run_data_turn(self, request: str):
        requests.append(request)

    runner = AgentTurnRunner(runtime)
    runner._run_ideator_lane = MethodType(run_lane, runner)
    runner.run_data_turn = MethodType(run_data_turn, runner)

    hypotheses = await runner.run_ideator_turn(2)

    assert [item.intervention for item in hypotheses] == [
        "change ideator-1-1",
        "change ideator-1-2",
    ]
    assert requests == ["- correlation between age and target"]


@pytest.mark.asyncio
async def test_gated_batch_preserves_eda_request(monkeypatch) -> None:
    """gated 模式的 IdeatorHypothesisBatch 也把 eda_request 传给动态 EDA。"""
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._ideation = "ideageneration"
    runtime._model = "m"
    runtime._store = object()
    runtime._supervisor = SimpleNamespace(state=SimpleNamespace(corpus_ref=None))

    async def fake_pipeline(drafts, **kwargs):
        return [_hypothesis("kept")]

    monkeypatch.setattr(
        "athena.research.agent_turn_runner.run_light_pipeline", fake_pipeline
    )
    runner = AgentTurnRunner(runtime)
    draft = IdeatorHypothesisDraft(
        statement="s",
        intervention="i",
        expected_effect="e",
        supported_premises=[],
        predicted_observations=["p"],
        disconfirming_observations=["d"],
    )
    batch = IdeatorHypothesisBatch(hypotheses=[draft], eda_request="mine X")

    out = await runner._finish_ideator_batch(batch)

    assert isinstance(out, HypothesisBatch)
    assert out.eda_request == "mine X"
    assert [item.intervention for item in out.hypotheses] == ["change kept"]
