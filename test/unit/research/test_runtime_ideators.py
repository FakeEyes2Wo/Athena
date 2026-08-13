"""Focused tests for concurrent Ideator fan-out and display projection."""

import asyncio
from dataclasses import dataclass
from types import MethodType, SimpleNamespace

import pytest

from athena.core.research_models import Hypothesis
from athena.research.runtime import ResearchRuntime


def _hypothesis(label: str) -> Hypothesis:
    return Hypothesis(
        statement=f"statement {label}",
        intervention=f"change {label}",
        expected_effect="improve metric",
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, (1,)),
        (2, (1, 1)),
        (4, (2, 1, 1)),
        (8, (3, 3, 2)),
    ],
)
def test_ideator_allocations_cap_workers_at_three(count, expected) -> None:
    assert ResearchRuntime._ideator_allocations(count) == expected


@pytest.mark.asyncio
async def test_ideator_turn_runs_actual_lane_count_concurrently_and_merges_in_order(
    tmp_path,
) -> None:
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._provider = object()
    runtime._state = SimpleNamespace(eda_dir=str(tmp_path))
    runtime._registry = SimpleNamespace(contains=lambda _name: True)
    started: list[tuple[str, int]] = []
    all_started = asyncio.Event()

    async def run_lane(self, label: str, target: int, _eda_dir):
        started.append((label, target))
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        return [_hypothesis(label)] * target

    runtime._run_ideator_lane = MethodType(run_lane, runtime)

    hypotheses = await runtime._run_ideator_turn(4)

    assert started == [
        ("ideator-1", 2),
        ("ideator-2", 1),
        ("ideator-3", 1),
    ]
    assert [item.intervention for item in hypotheses] == [
        "change ideator-1",
        "change ideator-1",
        "change ideator-2",
        "change ideator-3",
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
    runtime._provider = object()
    runtime._state = SimpleNamespace(eda_dir=str(tmp_path))
    runtime._registry = SimpleNamespace(contains=lambda _name: True)
    errors: list[tuple[str, str]] = []

    async def run_lane(self, label: str, _target: int, _eda_dir):
        if label == "ideator-2":
            raise RuntimeError("offline")
        return [_hypothesis(label)]

    async def publish_output(*, source, channel, text, plan=None, **_kwargs):
        assert source == "agent"
        assert channel == "error"
        errors.append((plan, text))

    runtime._run_ideator_lane = MethodType(run_lane, runtime)
    runtime.publish_output = publish_output

    hypotheses = await runtime._run_ideator_turn(3)

    assert [item.intervention for item in hypotheses] == [
        "change ideator-1",
        "change ideator-3",
    ]
    assert errors == [("ideator-2", "Ideator 2 failed: offline")]


@pytest.mark.asyncio
async def test_ideator_turn_resolves_relative_eda_dir_against_athena(tmp_path) -> None:
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._provider = object()
    runtime._root = tmp_path
    runtime._athena = tmp_path / ".athena"
    workspace = runtime._athena / "workspaces" / "athena-abc123"
    workspace.mkdir(parents=True)
    runtime._state = SimpleNamespace(eda_dir="workspaces/athena-abc123")
    runtime._registry = SimpleNamespace(contains=lambda _name: True)
    resolved: list[str] = []

    async def run_lane(self, label: str, _target: int, eda_dir):
        resolved.append(str(eda_dir))
        return [_hypothesis(label)]

    runtime._run_ideator_lane = MethodType(run_lane, runtime)

    hypotheses = await runtime._run_ideator_turn(1)

    assert resolved == [str(workspace.resolve())]
    assert [item.intervention for item in hypotheses] == ["change ideator-1"]
