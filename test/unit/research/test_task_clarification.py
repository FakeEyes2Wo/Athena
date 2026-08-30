"""Hermetic tests for Supervisor-owned task clarification handoff writing."""

import pytest

from athena.research.runtime_control import (
    maybe_run_task_understanding,
    persist_task_clarification,
    render_task_clarification,
)


class FakeStore:
    def __init__(self) -> None:
        self.texts: dict[str, str] = {}

    async def put_text(self, text: str) -> str:
        ref = f"ref-{len(self.texts)}"
        self.texts[ref] = text
        return ref


class FakeState:
    def __init__(self, *, phase: str = "PREPARE", understanding=None) -> None:
        self.phase = phase
        self.task_understanding = understanding
        self.handoff_refs: dict[str, str] = {}
        self.saved: str | None = None

    def save(self, path: str) -> None:
        self.saved = path


class FakeSession:
    def __init__(self, qa: list[tuple[str, str]] | None = None) -> None:
        self.clarification_qa = list(qa or [])


def _runtime(*, understanding=None, qa=None, phase: str = "PREPARE"):
    state = FakeState(phase=phase, understanding=understanding)
    session = FakeSession(qa)
    store = FakeStore()

    class FakeAgentTurns:
        async def run_supervisor_turn(self, text: str) -> str:
            return "ok"

    class Rt:
        pass

    runtime = Rt()
    runtime._provider = object()
    runtime._task_text = "predict titanic survival"
    runtime._session = session
    runtime.state = state
    runtime.store = store
    runtime.state_path = "/tmp/state.json"
    runtime._agent_turns = FakeAgentTurns()

    async def publish_output(**_kwargs) -> None:
        return None

    runtime.publish_output = publish_output
    runtime.replay_output_events = lambda: []
    return runtime


@pytest.mark.asyncio
async def test_persist_task_clarification_writes_qa_and_understanding() -> None:
    runtime = _runtime(
        understanding={"title": "titanic", "target": "survived"},
        qa=[("which metric?", "choice:f1")],
    )

    await persist_task_clarification(runtime)

    ref = runtime.state.handoff_refs["task_clarification"]
    markdown = runtime.store.texts[ref]
    assert "which metric?" in markdown
    assert "choice:f1" in markdown
    assert "titanic" in markdown
    assert runtime.state.saved == "/tmp/state.json"


@pytest.mark.asyncio
async def test_maybe_run_task_understanding_records_handoff_after_supervisor_turn() -> None:
    runtime = _runtime(qa=[])

    async def fake_turn(_text: str) -> str:
        runtime.state.task_understanding = {"title": "titanic", "target": "survived"}
        runtime._session.clarification_qa.append(("which metric?", "choice:f1"))
        return "ok"

    runtime._agent_turns.run_supervisor_turn = fake_turn  # type: ignore[method-assign]

    await maybe_run_task_understanding(runtime)

    assert runtime.state.task_understanding == {
        "title": "titanic",
        "target": "survived",
    }
    ref = runtime.state.handoff_refs["task_clarification"]
    markdown = runtime.store.texts[ref]
    assert "which metric?" in markdown
    assert "choice:f1" in markdown


@pytest.mark.asyncio
async def test_maybe_run_task_understanding_restores_handoff_when_skipping() -> None:
    runtime = _runtime(understanding={"title": "titanic"}, qa=[])

    await maybe_run_task_understanding(runtime)

    assert "task_clarification" in runtime.state.handoff_refs


def test_render_task_clarification_contains_sections() -> None:
    markdown = render_task_clarification(
        "task", [("q?", "a")], {"title": "t"}
    )
    assert "# TASK_CLARIFICATION" in markdown
    assert "## Original task" in markdown
    assert "## Clarification Q&A" in markdown
    assert "## Final understanding" in markdown
