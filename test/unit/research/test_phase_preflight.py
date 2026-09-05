"""Phase entry must verify confirmed context before doing any work."""

from types import MethodType, SimpleNamespace

import pytest

from athena.research.clarification import context as task_context
from athena.research.prepare import orchestrator as prepare_phase
from athena.research.runtime.phase_runner import PhaseRunner
from athena.research.turns.runner import AgentTurnRunner


class _RejectingProvider:
    async def load(self):
        raise task_context.ConfirmedTaskContextError("corrupt")


def test_task_prompt_prepends_context() -> None:
    assert task_context.task_prompt("task", "contract") == "contract\n\ntask"


@pytest.mark.asyncio
async def test_ungated_legacy_context_may_be_absent(monkeypatch) -> None:
    runtime = SimpleNamespace(
        state=SimpleNamespace(task_understanding=None),
        config=SimpleNamespace(
            research=SimpleNamespace(task=SimpleNamespace(confirmation_gate=False))
        ),
    )
    monkeypatch.setattr(
        task_context.ConfirmedTaskContextProvider,
        "from_runtime",
        lambda _runtime: _RejectingProvider(),
    )

    assert await task_context.confirmed_task_context_block(runtime) == ""


@pytest.mark.asyncio
async def test_prepare_preflight_runs_before_custom_phase(monkeypatch) -> None:
    called = False

    async def custom_phase():
        nonlocal called
        called = True

    runtime = SimpleNamespace(
        prepare_phase=custom_phase,
        config=SimpleNamespace(
            research=SimpleNamespace(task=SimpleNamespace(confirmation_gate=True))
        ),
    )
    monkeypatch.setattr(
        task_context.ConfirmedTaskContextProvider,
        "from_runtime",
        lambda _runtime: _RejectingProvider(),
    )

    with pytest.raises(RuntimeError, match="confirmed task handoff"):
        await prepare_phase.run_prepare_phase(runtime, None)

    assert called is False


@pytest.mark.asyncio
async def test_prepare_override_is_rejected_for_registered_provider(
    monkeypatch,
) -> None:
    """A registered provider cannot replace the authoritative PREPARE gate."""
    monkeypatch.setattr(
        prepare_phase, "confirmed_task_context_block", lambda _runtime: _empty_context()
    )

    async def _empty_context() -> str:
        return ""

    called = False

    async def custom_phase():
        nonlocal called
        called = True

    runtime = SimpleNamespace(
        prepare_phase=custom_phase,
        provider=object(),
        task_confirmation_gate=False,
    )
    with pytest.raises(RuntimeError, match="authoritative PREPARE gate"):
        await prepare_phase.run_prepare_phase(runtime, None)
    assert called is False


@pytest.mark.asyncio
async def test_validation_preflight_runs_before_custom_phase(monkeypatch) -> None:
    called = False

    async def custom_phase(_commit: str, _metric: float):
        nonlocal called
        called = True

    runtime = SimpleNamespace(
        validation_phase=custom_phase,
        config=SimpleNamespace(
            research=SimpleNamespace(task=SimpleNamespace(confirmation_gate=True))
        ),
    )
    monkeypatch.setattr(
        task_context.ConfirmedTaskContextProvider,
        "from_runtime",
        lambda _runtime: _RejectingProvider(),
    )

    with pytest.raises(RuntimeError, match="confirmed task handoff"):
        await PhaseRunner(runtime).run_validation_phase("commit", 0.5)

    assert called is False


@pytest.mark.asyncio
async def test_ideator_preflight_runs_before_handoff(monkeypatch) -> None:
    handoff_started = False
    runtime = SimpleNamespace(
        provider=object(),
        config=SimpleNamespace(
            research=SimpleNamespace(task=SimpleNamespace(confirmation_gate=True))
        ),
    )
    runner = AgentTurnRunner(runtime)

    async def collect_handoffs(self):
        nonlocal handoff_started
        handoff_started = True
        return []

    runner._collect_handoff_texts = MethodType(collect_handoffs, runner)
    monkeypatch.setattr(
        task_context.ConfirmedTaskContextProvider,
        "from_runtime",
        lambda _runtime: _RejectingProvider(),
    )

    with pytest.raises(RuntimeError, match="confirmed task handoff"):
        await runner.run_ideator_turn(1)

    assert handoff_started is False
