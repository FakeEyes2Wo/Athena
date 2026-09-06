"""Behavior contracts for PREPARE handoff event forwarding."""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.research.runtime.phase_runner import PhaseRunner


class _Events:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    async def project_agent_event(self, agent_id, kind, ref, data=None) -> None:
        self.seen.append((agent_id, kind))


class _Agents:
    def __init__(self) -> None:
        self.reaped: list[str] = []

    def has_agent(self, agent_id: str) -> bool:
        return False

    async def create_root(self, agent_type, request, *, agent_id, name):
        return agent_id, "run-1"

    async def wait_run(self, run_id):
        return SimpleNamespace(
            status="COMPLETED",
            response_ref=None,
            result_ref=None,
            error=None,
        )

    async def run_events(self, run_id, after_sequence=0):
        for kind in ("agent/text_delta", "run/completed"):
            yield SimpleNamespace(kind=kind, event_ref="ref", data={})

    async def reap(self, agent_id: str) -> None:
        self.reaped.append(agent_id)


@pytest.mark.asyncio
async def test_handoff_forwards_events_reads_output_and_reaps(tmp_path: Path) -> None:
    events, agents = _Events(), _Agents()
    (tmp_path / "EDA_INDEX.md").write_text("# index\n", encoding="utf-8")
    runtime = SimpleNamespace(agents=agents, store=object(), events=events)
    runner = PhaseRunner(runtime)

    text = await runner._run_handoff_agent(
        agent_id="prepare_eda",
        workspace=tmp_path,
        output_file="EDA_INDEX.md",
        content="finalize",
        reap_after=True,
    )

    assert text == "# index\n"
    assert not hasattr(runner, "__dict__")
    assert events.seen == [
        ("prepare_eda", "agent/text_delta"),
        ("prepare_eda", "run/completed"),
    ]
    assert agents.reaped == ["prepare_eda"]


def test_project_agent_event_is_async() -> None:
    from athena.research.runtime.events import RuntimeEvents

    assert asyncio.iscoroutinefunction(RuntimeEvents.project_agent_event)


def test_no_event_callback_is_left_synchronous() -> None:
    source_root = Path(__file__).resolve().parents[3] / "src" / "athena"
    offenders: list[str] = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "wait_run_events" not in text:
            continue
        for match in re.finditer(r"^(\s*)def publish\(", text, re.MULTILINE):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(source_root)}:{line}")

    assert not offenders, (
        "wait_run_events callbacks must be async; synchronous callbacks: "
        f"{offenders}"
    )
