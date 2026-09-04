"""Two-event runtime projection and tool-output safety tests."""

import json

import pytest
from pydantic import ValidationError

from athena.core.artifact_store import LocalArtifactStore
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment
from athena.core.workspace import GitWorkBranch
from athena.execution.runtime import CommandResult
from athena.research.runtime import ResearchRuntime
from athena.research.runtime.event_projection import supervisor_state
from athena.research.supervisor.events import (
    EventProjector,
    OutputEvent,
    StateEvent,
    redact,
    truncate_middle,
)
from athena.research.supervisor.plans import PlanState


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


@pytest.mark.asyncio
async def test_tool_preview_and_spill_strip_terminal_control_sequences(
    tmp_path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    raw = (
        "\x1b[36m进度\x1b[0m\r刷新\r\n"
        "\x1b]0;private title\x07结果\x9b31m!\x9b0m\n"
        "保留\tUnicode Ω\x00\x08\x7f�\n" + ("尾声" * 300)
    )

    event = await EventProjector(store).tool_output(stdout=raw)

    full = await store.get_text(event.artifact_ref)
    expected_prefix = "进度\n刷新\n结果!\n保留\tUnicode Ω\n"
    assert event.text.startswith(expected_prefix)
    assert full.startswith(expected_prefix)
    assert "\x1b" not in event.text
    assert "\x1b" not in full
    assert "\x9b" not in full
    assert "\r" not in full
    assert "\x00" not in full
    assert "\x08" not in full
    assert "\x7f" not in full
    assert "�" not in full
    assert event.truncated is True


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


def test_truncate_middle_leaves_short_text_untouched() -> None:
    assert truncate_middle("inspect data", 200) == "inspect data"


def test_truncate_middle_preserves_head_and_tail_with_marker() -> None:
    command = "python train.py --epochs 100 --batch-size 128 --save-dir ./output"
    result = truncate_middle(command, 40)

    assert result.startswith("python train.py")
    assert result.endswith("--save-dir ./output")
    assert "chars truncated" in result


def test_truncate_middle_reports_removed_char_count() -> None:
    result = truncate_middle("a" * 100, 60)

    assert "40 chars truncated" in result


def test_truncate_middle_is_unicode_safe() -> None:
    result = truncate_middle("命令" * 50, 20)

    assert "chars truncated" in result
    assert result.encode("utf-8").decode("utf-8") == result


def test_output_sequence_is_process_local_and_monotonic(tmp_path) -> None:
    projector = EventProjector(LocalArtifactStore(tmp_path / "artifacts"))

    first = projector.output(source="agent", channel="text", text="one")
    second = projector.output(source="supervisor", channel="text", text="two")

    assert (first.seq, second.seq) == (1, 2)
    assert (first.session_id, first.scope, first.scope_id) == (None, None, None)


def test_output_scope_metadata_is_atomic_and_redacted(tmp_path) -> None:
    projector = EventProjector(LocalArtifactStore(tmp_path / "artifacts"))
    event = projector.output(
        source="agent",
        channel="text",
        text="api_key=secret-value",
        session_id="session-1",
        scope="task_understanding",
        scope_id="draft-1",
    )

    assert (event.session_id, event.scope, event.scope_id) == (
        "session-1",
        "task_understanding",
        "draft-1",
    )
    assert "secret-value" not in event.text


@pytest.mark.parametrize(
    "metadata",
    [
        {"session_id": "session-1"},
        {"scope": "task_understanding", "scope_id": "draft-1"},
        {"session_id": "session-1", "scope": "", "scope_id": "draft-1"},
    ],
)
def test_output_rejects_partial_scope_metadata(tmp_path, metadata) -> None:
    projector = EventProjector(LocalArtifactStore(tmp_path / "artifacts"))
    with pytest.raises(ValidationError):
        projector.output(source="agent", channel="text", text="safe", **metadata)


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
        "manual",
        "pending",
        "validation",
        "eda_dir",
        "task_understanding",
        "resume_available",
        "resume_reason",
    }
    assert event.manual is False
    assert event.pending == []
    assert event.validation is None
    assert event.eda_dir is None
    assert event.resume_available is False
    assert event.resume_reason is None


@pytest.mark.parametrize(
    ("status", "phase", "task", "understanding", "available", "reason"),
    [
        ("IDLE", "PREPARE", "original", None, True, "interrupted"),
        ("WAITING", "SEARCH", "original", None, True, "paused"),
        ("FAILED", "VALIDATE", "original", None, True, "failed"),
        ("RUNNING", "SEARCH", "original", None, False, "already_running"),
        ("STOPPED", "SEARCH", "original", None, False, "stopped"),
        ("COMPLETED", "COMPLETED", "original", None, False, "completed"),
        ("IDLE", "PREPARE", None, {"title": "legacy task"}, True, "interrupted"),
        ("IDLE", "PREPARE", None, None, False, "no_task"),
    ],
)
def test_state_projection_advertises_durable_resume_capability(
    tmp_path,
    status: str,
    phase: str,
    task: str | None,
    understanding: dict[str, str] | None,
    available: bool,
    reason: str,
) -> None:
    """A snapshot exposes the persisted resume decision, including legacy tasks."""
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.state.status = status
    runtime.state.phase = phase
    runtime.state.task_text = task
    runtime.state.task_understanding = understanding

    event = supervisor_state(runtime.supervisor, 0)

    assert event.resume_available is available
    assert event.resume_reason == reason


def test_state_event_defaults_resume_fields_for_older_producers() -> None:
    """Older complete state payloads cannot inherit resume state from another session."""
    event = StateEvent.model_validate(
        {
            "type": "state",
            "status": "IDLE",
            "phase": "PREPARE",
            "plans": [],
            "search": {},
            "sota": None,
            "waiting": None,
        }
    )

    assert event.resume_available is False
    assert event.resume_reason is None


@pytest.mark.asyncio
async def test_runtime_projects_agent_text_callback_to_output(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.events.project_agent_event(
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
async def test_runtime_projects_llm_tool_llm_events_in_arrival_order(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.events.project_agent_event(
        "prepare",
        "agent/function_call",
        "event:call-1",
        {"name": "shell_command", "arguments": {"command": "inspect data"}},
    )
    await runtime.events.project_agent_event(
        "prepare",
        "command/completed",
        "event:tool-1",
        CommandResult(
            ok=True,
            stdout="tool result",
            stderr="",
            exit_code=0,
            truncated=False,
        ).to_dict(),
    )
    await runtime.events.project_agent_event(
        "prepare",
        "agent/text_delta",
        "event:text-1",
        {"delta": "continue analysis", "accumulated": "continue analysis"},
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert [event["source"] for event in outputs] == ["agent", "tool", "agent"]
    assert outputs[0]["text"] == 'shell_command({"command": "inspect data"})'
    assert outputs[0]["tool"] == "shell_command"
    assert outputs[1]["text"] == "tool result"
    assert outputs[2]["text"] == "continue analysis"


@pytest.mark.asyncio
async def test_runtime_does_not_invent_agent_text_before_a_tool(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.events.project_agent_event(
        "prepare",
        "agent/function_call",
        "event:call-1",
        {"name": "shell_command", "arguments": {"command": "inspect data"}},
    )
    await runtime.events.project_agent_event(
        "prepare",
        "command/completed",
        "event:tool-1",
        CommandResult(
            ok=True,
            stdout="tool result",
            stderr="",
            exit_code=0,
            truncated=False,
        ).to_dict(),
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert [(event["source"], event["text"]) for event in outputs] == [
        ("agent", 'shell_command({"command": "inspect data"})'),
        ("tool", "tool result"),
    ]


@pytest.mark.asyncio
async def test_long_shell_command_is_middle_truncated_in_function_call(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    command = "python train.py " + "--very-long-argument " * 40
    await runtime.events.project_agent_event(
        "prepare",
        "agent/function_call",
        "event:call-1",
        {"name": "shell_command", "arguments": {"command": command, "timeout_s": 120}},
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert len(outputs) == 1
    text = outputs[0]["text"]
    assert "chars truncated" in text
    assert text.startswith('shell_command({"command": "python train.py')
    assert '"timeout_s": 120' in text
    assert command not in text  # 完整超长命令不再原样刷屏


@pytest.mark.asyncio
async def test_ideator_projects_only_llm_text_and_hides_tool_calls(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.events.project_agent_event(
        "ideator-1",
        "agent/function_call",
        "event:call-1",
        {"name": "read_file", "arguments": {"path": "eda.csv"}},
    )
    await runtime.events.project_agent_event(
        "ideator-1",
        "command/completed",
        "event:tool-1",
        CommandResult(
            ok=True, stdout="tool result", stderr="", exit_code=0, truncated=False
        ).to_dict(),
    )
    await runtime.events.project_agent_event(
        "ideator-1",
        "agent/text_delta",
        "event:text-1",
        {"delta": "eda shows drift\n", "accumulated": "eda shows drift\n"},
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert [event["source"] for event in outputs] == ["agent"]
    assert outputs[0]["text"] == "eda shows drift"
    assert outputs[0]["tool"] is None


@pytest.mark.asyncio
async def test_ideator_trailing_newline_is_stripped_from_text(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.events.project_agent_event(
        "ideator-2",
        "agent/text_delta",
        "event:text-1",
        {"delta": "first token\n"},
    )
    await runtime.events.project_agent_event(
        "ideator-2",
        "agent/text_delta",
        "event:text-2",
        {"delta": "second token\n"},
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert [event["text"] for event in outputs] == ["first token", "second token"]


@pytest.mark.asyncio
async def test_streamed_agent_text_is_persisted_as_complete_messages(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    await runtime.events.project_agent_event(
        "hyp_1",
        "agent/text_delta",
        "event:text-1",
        {"delta": "hello", "accumulated": "hello"},
    )
    await runtime.events.project_agent_event(
        "hyp_1",
        "agent/text_delta",
        "event:text-2",
        {"delta": " world", "accumulated": "hello world"},
    )
    await runtime.events.project_agent_event(
        "hyp_1",
        "agent/function_call",
        "event:call-1",
        {"name": "shell_command", "arguments": {"command": "ls"}},
    )
    await runtime.events.project_agent_event(
        "hyp_1",
        "agent/text_delta",
        "event:text-3",
        {"delta": "password=secret123 done", "accumulated": "password=secret123 done"},
    )
    await runtime.events.project_agent_event(
        "hyp_1",
        "turn_completed",
        "event:end",
        {},
    )

    replayed = runtime.replay_output_events()
    # Two complete Agent messages are persisted around the tool call.
    agent_text = [
        record["text"]
        for record in replayed
        if record.get("type") == "output"
        and record.get("source") == "agent"
        and record.get("channel") == "text"
        and record.get("tool") is None
    ]
    assert agent_text == ["hello world", "password=[REDACTED] done"]
    # The first message is flushed before the tool call; redaction applies.
    assert replayed[1]["tool"] == "shell_command"
    assert all("secret123" not in text for text in agent_text)


def test_state_projection_includes_announced_ideator_lane_count(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.events.set_ideator_lanes(3)

    event = supervisor_state(runtime.supervisor, 3)

    assert event.search["ideator_lanes"] == 3


@pytest.mark.asyncio
async def test_control_transition_publishes_complete_state(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.message("/pause")

    assert [kind for kind, _payload in seen] == ["state", "state"]
    assert seen[-1][1]["status"] == "WAITING"


@pytest.mark.asyncio
async def test_manual_and_auto_commands_toggle_mode(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.subscribe(lambda _kind, _payload: None)

    assert await runtime.message("/manual") == "manual mode on"
    assert runtime.state.manual_mode is True
    assert await runtime.message("/auto") == "manual mode off"
    assert runtime.state.manual_mode is False


@pytest.mark.asyncio
async def test_search_configuration_publishes_complete_state(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.supervisor.configure_search(search_limit=6, concurrency=2)

    assert [kind for kind, _payload in seen] == ["state", "state"]
    assert seen[-1][1]["search"]["limit"] == 6
    assert seen[-1][1]["search"]["concurrency"] == 2


@pytest.mark.asyncio
async def test_supervisor_bare_output_is_projected_with_seq(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.events.publish_from_supervisor(
        "output",
        {
            "source": "supervisor",
            "channel": "text",
            "text": "PREPARE completed with trusted metric 0.83.",
        },
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert len(outputs) == 1
    event = OutputEvent.model_validate(outputs[0])  # 不再缺 seq
    assert event.seq >= 1
    assert event.text == "PREPARE completed with trusted metric 0.83."


@pytest.mark.asyncio
async def test_supervisor_bare_output_without_text_still_projects(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))

    await runtime.events.publish_from_supervisor(
        "output", {"source": "supervisor", "channel": "text"}
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert len(outputs) == 1
    event = OutputEvent.model_validate(outputs[0])
    assert event.text == ""


@pytest.mark.asyncio
async def test_supervisor_output_preserves_generic_scope_and_identity(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))
    artifact_ref = await runtime.store.put_text("artifact payload")

    await runtime.events.publish_from_supervisor(
        "output",
        {
            "source": "agent",
            "channel": "text",
            "text": "api_key=secret-value",
            "message_id": "message-1",
            "plan": "plan-1",
            "tool": "report_task_understanding",
            "artifact_ref": artifact_ref,
            "truncated": True,
            "session_id": "session-1",
            "scope": "task_understanding",
            "scope_id": "draft-1",
        },
    )

    event = next(payload for kind, payload in seen if kind == "output")
    assert event["message_id"] == "message-1"
    assert event["plan"] == "plan-1"
    assert event["tool"] == "report_task_understanding"
    assert event["artifact_ref"] == artifact_ref
    assert event["truncated"] is True
    assert (
        event["session_id"],
        event["scope"],
        event["scope_id"],
    ) == ("session-1", "task_understanding", "draft-1")
    assert event["text"] == "api_key=[REDACTED]"


@pytest.mark.asyncio
async def test_supervisor_output_rejects_partial_scope_metadata(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    with pytest.raises(ValidationError):
        await runtime.events.publish_from_supervisor(
            "output",
            {
                "source": "agent",
                "channel": "text",
                "text": "safe",
                "session_id": "session-1",
            },
        )


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

    event = supervisor_state(runtime.supervisor, 0)

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
    runtime.state.plans[active_id] = PlanState(
        kind="SEARCH",
        context_ref="sha256:" + "4" * 64,
        turns_used=1,
        turn_limit=8,
        patience=3,
    )

    event = supervisor_state(runtime.supervisor, 0)

    assert event.search["attempts"] == 1
    assert event.search["successes"] == 0


def test_state_waiting_projection_has_exact_plan_ids_and_reason(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.state.plans["hyp_wait"] = PlanState(
        kind="SEARCH",
        context_ref="sha256:" + "5" * 64,
        turns_used=8,
        turn_limit=8,
        patience=3,
    )

    event = supervisor_state(runtime.supervisor, 0)

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
    output_ref = await runtime.store.put_text("complete redacted output")

    await runtime.events.project_agent_event(
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


@pytest.mark.asyncio
async def test_completed_command_replaces_an_unsanitized_full_output_ref(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict]] = []
    runtime.subscribe(lambda kind, payload: seen.append((kind, payload)))
    unsafe_ref = await runtime.store.put_text("\x1b[32mcomplete\x1b[0m\r\nnext\x00�")

    await runtime.project_command_result(
        CommandResult(
            ok=True,
            stdout="\x1b[32mcomplete\x1b[0m\r\nnext\x00�",
            stderr="",
            exit_code=0,
            truncated=True,
            output_ref=unsafe_ref,
        )
    )

    event = next(payload for kind, payload in seen if kind == "output")
    assert event["text"] == "complete\nnext"
    assert event["artifact_ref"] != unsafe_ref
    assert await runtime.store.get_text(event["artifact_ref"]) == "complete\nnext"


def _state_json(eda_dir: str | None) -> str:
    return json.dumps(
        {
            "status": "RUNNING",
            "phase": "SEARCH",
            "search_limit": 10,
            "concurrency": 4,
            "manual_mode": False,
            "plans": {},
            "validation": None,
            "eda_dir": eda_dir,
        }
    )


def test_init_preserves_relative_eda_dir_within_project(tmp_path) -> None:
    """相对项目根的 eda_dir（本项目自己的 workspace）不得被 resume 守卫清空。"""
    athena = tmp_path / ".athena"
    athena.mkdir()
    (athena / "state.json").write_text(_state_json("workspaces/eda"), encoding="utf-8")

    runtime = ResearchRuntime(project_root=tmp_path)

    assert runtime.state.eda_dir == "workspaces/eda"


def test_init_nulls_stale_absolute_eda_dir_outside_project(tmp_path) -> None:
    """跨目录拷贝来的绝对 eda_dir（指向别的项目）仍应被清空。"""
    athena = tmp_path / ".athena"
    athena.mkdir()
    stale = tmp_path.parent / "other" / ".athena" / "workspaces" / "athena-xyz"
    (athena / "state.json").write_text(_state_json(str(stale)), encoding="utf-8")

    runtime = ResearchRuntime(project_root=tmp_path)

    assert runtime.state.eda_dir is None
