"""Focused tests for concurrent Ideator fan-out and display projection."""

import asyncio
from dataclasses import dataclass
from types import MethodType, SimpleNamespace

import pytest

from athena.core.research_models import Hypothesis, HypothesisBatch
from athena.research.config import (
    ProviderConfig,
    ResearchOptions,
    RuntimeDependencies,
    SearchLimits,
)
from athena.research.idea_generation.idea_schemas import (
    IdeatorHypothesisBatch,
    IdeatorHypothesisDraft,
)
from athena.research.runtime import ResearchRuntime
from athena.research.turns.runner import AgentTurnRunner


def _hypothesis(label: str) -> Hypothesis:
    return Hypothesis(
        statement=f"statement {label}",
        intervention=f"change {label}",
        expected_effect="improve metric",
    )


def _runtime(
    tmp_path,
    *,
    eda_dir: str,
    ideator_count: int = 3,
    hypotheses_per_ideator: int = 2,
) -> ResearchRuntime:
    """Build a real runtime and set only the state exercised by these tests."""
    runtime = ResearchRuntime(
        project_root=tmp_path,
        research=ResearchOptions(
            search=SearchLimits(
                ideator_count=ideator_count,
                hypotheses_per_ideator=hypotheses_per_ideator,
            )
        ),
    )
    runtime.session.lifecycle.provider = object()
    runtime.state.eda_dir = eda_dir
    return runtime


class _LaneAgents:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def create_root(self, _agent_type, request, *, name, agent_id=None):
        self.requests.append(request)
        return agent_id or name, f"run-{name}"

    async def reap(self, _agent_id) -> None:
        pass


def _lane_runner(monkeypatch, batches: list[HypothesisBatch]):
    agents = _LaneAgents()
    runtime = SimpleNamespace(
        agents=agents,
        ideation="baseline",
        store=object(),
        supervisor=SimpleNamespace(evaluator_ref=None),
        state=SimpleNamespace(corpus_ref=None),
    )
    pending = iter(batches)

    async def no_task_context(_runtime):
        return ""

    async def no_eval_handoff(_store, _ref):
        return ""

    async def completed_run(*_args, **_kwargs):
        return SimpleNamespace(error=None)

    async def next_batch(_summary, _store, _schema):
        return next(pending)

    async def accept_batch(self, batch, *, rejections=None):
        return batch

    monkeypatch.setattr(
        "athena.research.turns.ideator.confirmed_task_context_block",
        no_task_context,
    )
    monkeypatch.setattr(
        "athena.research.turns.ideator.read_eval_handoff", no_eval_handoff
    )
    monkeypatch.setattr(
        "athena.research.turns.ideator._wait_run_with_heartbeat", completed_run
    )
    monkeypatch.setattr("athena.research.turns.ideator.load_agent_result", next_batch)
    runner = AgentTurnRunner(runtime)
    runner._finish_ideator_batch = MethodType(accept_batch, runner)
    return runner, agents


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
async def test_ideator_request_allows_writes_only_in_its_lane(
    monkeypatch, tmp_path
) -> None:
    runner, agents = _lane_runner(
        monkeypatch,
        [HypothesisBatch(hypotheses=[_hypothesis("lane")])],
    )

    await runner._run_ideator_lane("ideator-2-1", 1, tmp_path)

    content = agents.requests[0]["content"]
    assert "diagnostic scripts, figures, and evidence notes" in content
    assert "only under exploration/ideator-2-1/" in content
    assert (
        "keep every PREPARE, EDA, baseline, split, and evaluator file unchanged"
        in content
    )
    assert (tmp_path / "exploration" / "ideator-2-1").is_dir()


@pytest.mark.asyncio
async def test_accepted_ideator_batch_is_written_to_lane_result(
    monkeypatch, tmp_path
) -> None:
    batch = HypothesisBatch(hypotheses=[_hypothesis("accepted")])
    runner, _agents = _lane_runner(monkeypatch, [batch])

    accepted = await runner._run_ideator_lane("ideator-3-2", 1, tmp_path)

    result_path = tmp_path / "exploration" / "ideator-3-2" / "result.json"
    saved = HypothesisBatch.model_validate_json(result_path.read_text(encoding="utf-8"))
    assert saved.model_dump() == accepted.model_dump()


@pytest.mark.asyncio
async def test_ideator_lane_results_do_not_overwrite_each_other(
    monkeypatch, tmp_path
) -> None:
    first = HypothesisBatch(hypotheses=[_hypothesis("first")])
    second = HypothesisBatch(hypotheses=[_hypothesis("second")])
    runner, _agents = _lane_runner(monkeypatch, [first, second])

    await runner._run_ideator_lane("ideator-4-1", 1, tmp_path)
    await runner._run_ideator_lane("ideator-4-2", 1, tmp_path)

    first_path = tmp_path / "exploration" / "ideator-4-1" / "result.json"
    second_path = tmp_path / "exploration" / "ideator-4-2" / "result.json"
    saved_first = HypothesisBatch.model_validate_json(
        first_path.read_text(encoding="utf-8")
    )
    saved_second = HypothesisBatch.model_validate_json(
        second_path.read_text(encoding="utf-8")
    )
    assert saved_first.hypotheses[0].intervention == "change first"
    assert saved_second.hypotheses[0].intervention == "change second"


@pytest.mark.asyncio
async def test_ideator_turn_runs_actual_lane_count_concurrently_and_merges_in_order(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, eda_dir=str(tmp_path))
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
    from athena.research.supervisor.events import wait_run_events

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
    runtime = _runtime(tmp_path, eda_dir=str(tmp_path))
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
    # cd899a5 起，失败的一路会连 traceback 一起报出来（与 d4605d1 同一取向：
    # 失败的原因必须能到达看它的人）。断言退化为"标签 + 原因 + 附了 traceback"，
    # 而不是逐字相等——否则每次错误信息变详细都要改测试。
    assert len(errors) == 1
    plan, text = errors[0]
    assert plan == "ideator-1-2"
    assert text.startswith("Ideator 2 failed: offline")
    assert "RuntimeError: offline" in text


@pytest.mark.asyncio
async def test_ideator_turn_keeps_every_generated_hypothesis_without_truncation(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, eda_dir=str(tmp_path))

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
    workspace = tmp_path / "workspaces" / "eda"
    workspace.mkdir(parents=True)
    runtime = _runtime(tmp_path, eda_dir="workspaces/eda")
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
    runtime = _runtime(
        tmp_path,
        eda_dir=str(tmp_path),
        ideator_count=2,
        hypotheses_per_ideator=1,
    )
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
async def test_gated_batch_preserves_eda_request(monkeypatch, tmp_path) -> None:
    """gated 模式的 IdeatorHypothesisBatch 也把 eda_request 传给动态 EDA。"""
    runtime = ResearchRuntime(
        project_root=tmp_path,
        dependencies=RuntimeDependencies(provider=ProviderConfig(model="m")),
    )

    async def fake_pipeline(drafts, **kwargs):
        return [_hypothesis("kept")]

    monkeypatch.setattr(
        "athena.research.turns.ideator.run_light_pipeline", fake_pipeline
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
