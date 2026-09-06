"""SupervisorAgent decision and infrastructure boundaries."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest

from athena.agents.supervisor_agent import register_supervisor_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import RunStatus
from athena.core.artifact_store import LocalArtifactStore


def _tool_returns(messages: list[object]) -> str:
    return "\n".join(
        str(getattr(part, "content", ""))
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if getattr(part, "part_kind", None) == "tool-return"
    )


class _ToolThenAnswerProvider:
    model_name = "supervisor-test"

    def __init__(self, name: str, arguments: dict[str, object]) -> None:
        self.name = name
        self.arguments = arguments
        self.calls = 0
        self.tool_returns = ""

    async def stream(self, _config, _tools, messages, _cancel, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": "decision-1",
                    "name": self.name,
                    "arguments": self.arguments,
                },
            )
        else:
            self.tool_returns = _tool_returns(messages)
            yield StreamEvent(
                kind="text_delta",
                data={
                    "delta": '{"answer":"Recorded."}',
                    "accumulated": '{"answer":"Recorded."}',
                },
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


class _Actions:
    def __init__(self) -> None:
        self.proposals: list[dict[str, object]] = []

    async def propose_hypothesis(self, **payload: object) -> dict[str, object]:
        self.proposals.append(payload)
        return {"hypothesis_id": "hyp_vit"}

    async def select_next_hypothesis(self, hypothesis_id: str) -> dict[str, object]:
        if hypothesis_id != "hyp_known":
            raise KeyError(f"unknown hypothesis id: {hypothesis_id}")
        return {"selected": hypothesis_id}

    async def configure_search(self, **payload: object) -> dict[str, object]:
        return payload

    async def update_waiting_plan_budget(self, **payload: object) -> dict[str, object]:
        return payload

    async def set_phase_decision(self, decision: str) -> dict[str, object]:
        return {"decision": decision}

    async def record_guidance(self, text: str, scope: str) -> dict[str, object]:
        return {"text": text, "scope": scope}


async def _run_supervisor(
    tmp_path: Path, provider: _ToolThenAnswerProvider, actions: _Actions
) -> tuple[AgentRuntime, dict[str, object]]:
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    register_supervisor_agent(
        registry,
        provider=provider,
        artifacts=store,
        actions=actions,
    )
    runtime = AgentRuntime(
        type_registry=registry,
        project_root=tmp_path,
        rollout_dir=tmp_path / ".athena" / "logs" / "agents",
    )
    runtime.start()
    agent_id, run_id = await runtime.create_root(
        "supervisor",
        {"content": "Please reason about the next experiment."},
        agent_id="supervisor",
    )
    summary = await runtime.wait_run(run_id, timeout=5)
    assert agent_id == "supervisor"
    assert summary.status is RunStatus.COMPLETED
    assert summary.response_ref is not None
    outer = json.loads(summary.response_ref)
    answer = json.loads(await store.get_text(outer["result_ref"]))
    return runtime, answer


@pytest.mark.asyncio
async def test_supervisor_registers_with_stable_id_and_structured_log(
    tmp_path: Path,
) -> None:
    actions = _Actions()
    provider = _ToolThenAnswerProvider(
        "propose_hypothesis",
        {
            "statement": "Attention can improve nonlinear interactions",
            "intervention": "Train a compact ViT ensemble",
            "expected_effect": "Increase validation accuracy",
            "turn_limit": 8,
            "patience": 3,
        },
    )

    runtime, answer = await _run_supervisor(tmp_path, provider, actions)

    assert answer == {"answer": "Recorded."}
    assert actions.proposals[0]["turn_limit"] == 8
    assert (tmp_path / ".athena" / "logs" / "agents" / "supervisor.jsonl").is_file()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_supervisor_tool_rejects_extra_fields_before_action(
    tmp_path: Path,
) -> None:
    actions = _Actions()
    provider = _ToolThenAnswerProvider(
        "propose_hypothesis",
        {
            "statement": "Try attention",
            "intervention": "Train ViT",
            "expected_effect": "Improve score",
            "turn_limit": 8,
            "patience": 3,
            "start_process": True,
        },
    )

    runtime, _answer = await _run_supervisor(tmp_path, provider, actions)

    assert actions.proposals == []
    assert "extra_forbidden" in provider.tool_returns
    await runtime.aclose()


@pytest.mark.asyncio
async def test_supervisor_tool_returns_unknown_id_error_to_same_turn(
    tmp_path: Path,
) -> None:
    actions = _Actions()
    provider = _ToolThenAnswerProvider(
        "select_next_hypothesis", {"hypothesis_id": "hyp_missing"}
    )

    runtime, _answer = await _run_supervisor(tmp_path, provider, actions)

    assert "unknown hypothesis id: hyp_missing" in provider.tool_returns
    assert provider.calls == 2
    await runtime.aclose()


@pytest.mark.asyncio
async def test_supervisor_rejects_assignment_beyond_configured_budget(
    tmp_path: Path,
) -> None:
    actions = _Actions()
    provider = _ToolThenAnswerProvider(
        "propose_hypothesis",
        {
            "statement": "Try attention",
            "intervention": "Train ViT",
            "expected_effect": "Improve score",
            "turn_limit": 13,
            "patience": 3,
        },
    )

    runtime, _answer = await _run_supervisor(tmp_path, provider, actions)

    assert actions.proposals == []
    assert "less than or equal to 12" in provider.tool_returns
    await runtime.aclose()
