"""Regression tests for PhaseRunner handoff event forwarding."""

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.agents.ideator_agent import HandoffResult
from athena.research import phase_runner
from athena.research.phase_runner import PhaseRunner


class _FakeAgents:
    def has_agent(self, agent_id: str) -> bool:
        return False

    async def create_root(
        self,
        agent_type: str,
        request: dict,
        *,
        agent_id: str,
        name: str,
    ) -> tuple[str, str]:
        return agent_id, "run-1"


class _FakeEventsBus:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict | None]] = []

    async def project_agent_event(
        self,
        agent_id: str,
        kind: str,
        ref: str,
        data: dict | None = None,
    ) -> None:
        self.calls.append((agent_id, kind, ref, data))


@pytest.mark.asyncio
async def test_handoff_publish_is_awaitable_and_awaits_event_bus(
    tmp_path: Path, monkeypatch
) -> None:
    output_file = "EDA_HANDOFF.md"
    (tmp_path / output_file).write_text("# EDA handoff\n", encoding="utf-8")
    events_bus = _FakeEventsBus()
    runtime = SimpleNamespace(
        _agents=_FakeAgents(),
        _events_bus=events_bus,
        _store=object(),
    )

    async def fake_wait_run_events(agents, run_id, publish):
        forwarded = publish("agent/text_delta", "sha256:event", {"delta": "ready"})
        assert inspect.isawaitable(forwarded)
        await forwarded
        assert events_bus.calls == [
            (
                "prepare-eda",
                "agent/text_delta",
                "sha256:event",
                {"delta": "ready"},
            )
        ]
        return SimpleNamespace(error=None)

    async def fake_load_agent_result(summary, store, result_type):
        return HandoffResult(summary="complete", handoff_file=output_file)

    monkeypatch.setattr(phase_runner, "wait_run_events", fake_wait_run_events)
    monkeypatch.setattr(phase_runner, "load_agent_result", fake_load_agent_result)

    handoff = await PhaseRunner(runtime)._run_handoff_agent(
        agent_id="prepare-eda",
        agent_type="prepare_eda",
        workspace=str(tmp_path),
        output_file=output_file,
        content="inspect the dataset",
    )

    assert handoff == "# EDA handoff\n"
    assert events_bus.calls == [
        ("prepare-eda", "agent/text_delta", "sha256:event", {"delta": "ready"})
    ]
