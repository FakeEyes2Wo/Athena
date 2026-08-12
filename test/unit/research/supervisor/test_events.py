"""Two-event runtime projection and tool-output safety tests."""

import pytest

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment
from athena.core.workspace import GitWorkBranch
from athena.research.supervisor.events import (
    EventProjector,
    OutputEvent,
    StateEvent,
    redact,
)
from athena.research.runtime import ResearchRuntime
from athena.research.supervisor.plans import PlanState
from athena.execution.runtime import CommandResult


@pytest.mark.asyncio
async def test_tool_preview_prefers_stderr_and_is_at_most_512_bytes(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    projector = EventProjector(store)
    for index in range(6):
        projector.output(source="agent", channel="text", text=str(index))

    event = await projector.tool_output(
        stdout="o" * 500,
        stderr="错误" * 200,
        tool="shell_command",
    )

    assert isinstance(event, OutputEvent)
    assert event.type == "output"
    assert event.seq == 7
    assert event.channel == "stderr"
    assert len(event.text.encode("utf-8")) <= 512
    assert event.text.encode("utf-8").decode("utf-8") == event.text
    assert event.truncated is True
    assert event.artifact_ref is not None


@pytest.mark.asyncio
async def test_preview_and_spilled_output_share_redaction(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    secret = "sk-this-must-not-leak"

    event = await EventProjector(store).tool_output(
        stdout=f"OPENAI_API_KEY={secret}\n" + ("x" * 700),
        stderr="",
    )

    full = await store.get_text(event.artifact_ref)
    assert secret not in event.text
    assert secret not in full
    assert "[REDACTED]" in event.text
    assert "[REDACTED]" in full


def test_redact_masks_secret_patterns() -> None:
    cleaned = redact(
        "api_key=private1 access_token=private2 password=private3 "
        "secret:private4 sk-livevalue-abcdef"
    )

    assert "private1" not in cleaned
    assert "private2" not in cleaned
    assert "private3" not in cleaned
    assert "private4" not in cleaned
    assert "sk-livevalue-abcdef" not in cleaned
    assert cleaned.count("[REDACTED]") == 5


def test_output_sequence_is_process_local_and_monotonic(tmp_path) -> None:
    projector = EventProjector(LocalArtifactStore(tmp_path / "artifacts"))

    first = projector.output(source="agent", channel="text", text="one")
    second = projector.output(source="supervisor", channel="text", text="two")

    assert (first.seq, second.seq) == (1, 2)


def test_state_event_is_a_complete_replaceable_snapshot() -> None:
    event = StateEvent(
        status="RUNNING",
        phase="SEARCH",
        plans=[],
        search={"attempts": 2, "limit": 10, "successes": 1, "concurrency": 4},
        sota={"experiment": "exp_1", "metric": 0.8},
        waiting=None,
    )

    assert event.type == "state"
    assert set(event.model_dump()) == {
        "type",
        "status",
        "phase",
        "plans",
        "search",
        "sota",
        "waiting",
    }


@pytest.mark.asyncio
async def test_runtime_projects_agent_text_callback_to_output(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime._project_agent_event(
        "hyp_1",
        "agent/text_delta",
        "event:turn_1",
        {"delta": "hello"},
    )

    assert seen[-1][0] == "output"
    assert seen[-1][1]["source"] == "agent"
    assert seen[-1][1]["text"] == "hello"
    assert seen[-1][1]["plan"] == "hyp_1"


@pytest.mark.asyncio
async def test_control_transition_publishes_complete_state(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.message("/pause")

    assert [kind for kind, _payload in seen] == ["state", "state"]
    assert seen[-1][1]["status"] == "WAITING"


@pytest.mark.asyncio
async def test_search_configuration_publishes_complete_state(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.supervisor.configure_search(search_limit=6, concurrency=2)

    assert [kind for kind, _payload in seen] == ["state", "state"]
    assert seen[-1][1]["search"]["limit"] == 6
    assert seen[-1][1]["search"]["concurrency"] == 2


def test_state_projection_reads_successes_and_sota_from_research_tree(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    hypothesis_id = runtime.tree.add_hypothesis(
        Hypothesis(
            id="hyp_1",
            statement="A calibrated model improves accuracy",
            intervention="fit calibrated model",
            expected_effect="higher accuracy",
        )
    )
    runtime.tree.add_experiment(
        "exp_1",
        Experiment(
            hypothesis_id=hypothesis_id,
            commit="abc123",
            plan=ExperimentPlan(
                kind="search",
                change="fit calibrated model",
                run_config_ref="sha256:" + "1" * 64,
                budget={},
                acceptance_rule="improve accuracy",
            ),
            gitwork=GitWorkBranch(
                path="worktrees/hyp_1",
                branch="search/hyp_1",
                base_commit="base123",
            ),
        ),
    )
    runtime.tree.transition_experiment("exp_1", "RUNNING")
    runtime.tree.complete_experiment(
        "exp_1",
        eval=EvalResult(
            experiment_id="exp_1",
            primary=0.84,
            per_sample="sha256:" + "2" * 64,
        ),
        verdict=None,
        artifacts={},
    )
    runtime.tree.set_sota("exp_1")

    event = runtime._state_event()

    assert event.search["attempts"] == 1
    assert event.search["successes"] == 1
    assert event.sota == {"experiment": "exp_1", "metric": 0.84, "commit": "abc123"}


def test_state_attempts_are_settled_experiments_plus_active_plans(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    active_id = runtime.tree.add_hypothesis(
        Hypothesis(
            id="hyp_active",
            statement="An active experiment is not settled",
            intervention="run active candidate",
            expected_effect="unknown until completion",
        )
    )
    runtime.tree.add_experiment(
        "exp_active",
        Experiment(
            hypothesis_id=active_id,
            commit="active123",
            plan=ExperimentPlan(
                kind="search",
                change="run active candidate",
                run_config_ref="sha256:" + "3" * 64,
                budget={},
                acceptance_rule="improve score",
            ),
            gitwork=GitWorkBranch(
                path="worktrees/hyp_active",
                branch="search/hyp_active",
                base_commit="base123",
            ),
        ),
    )
    runtime.research_state.plans[active_id] = PlanState(
        kind="SEARCH",
        context_ref="sha256:" + "4" * 64,
        turns_used=1,
        turn_limit=8,
        patience=3,
    )

    event = runtime._state_event()

    assert event.search["attempts"] == 1
    assert event.search["successes"] == 0


def test_state_waiting_projection_has_exact_plan_ids_and_reason(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.research_state.plans["hyp_wait"] = PlanState(
        kind="SEARCH",
        context_ref="sha256:" + "5" * 64,
        turns_used=8,
        turn_limit=8,
        patience=3,
    )

    event = runtime._state_event()

    assert event.waiting == {
        "plans": ["hyp_wait"],
        "reason": "turn_limit_exhausted",
    }


@pytest.mark.asyncio
async def test_completed_command_projects_one_safe_output_with_its_full_ref(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))
    output_ref = await runtime._store.put_text("complete redacted output")

    await runtime._project_agent_event(
        "hyp_1",
        "command/completed",
        "exec:out",
        CommandResult(
            ok=False,
            stdout="o" * 500,
            stderr="OPENAI_API_KEY=sk-this-must-not-leak\n" + ("错" * 200),
            exit_code=1,
            truncated=True,
            output_ref=output_ref,
        ).to_dict(),
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert len(outputs) == 1
    event = outputs[0]
    assert event["source"] == "tool"
    assert event["channel"] == "stderr"
    assert len(event["text"].encode("utf-8")) <= 512
    assert "sk-this-must-not-leak" not in event["text"]
    assert event["artifact_ref"] == output_ref
