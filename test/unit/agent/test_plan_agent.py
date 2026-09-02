"""PlanAgent structured-output and workspace-isolation contracts."""

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest

from athena.agents.task_agents import register_plan_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import RunStatus
from athena.core.artifact_store import LocalArtifactStore
from athena.execution.runtime import ExecutionRuntime


def _tool_returns(messages: list[object]) -> list[str]:
    return [
        str(getattr(part, "content", ""))
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if getattr(part, "part_kind", None) == "tool-return"
    ]


class _ProseProvider:
    model_name = "plan-prose-test"

    async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
        yield StreamEvent(
            kind="text_delta",
            data={"delta": "I am finished.", "accumulated": "I am finished."},
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


class _WorkspaceProvider:
    model_name = "plan-workspace-test"

    def __init__(self) -> None:
        self.completed_agents = 0

    async def stream(self, _config, _tools, messages, _cancel, **_kwargs):
        returns = _tool_returns(messages)
        if not returns:
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": "write",
                    "name": "write_file",
                    "arguments": {"path": "identity.txt", "content": "owned"},
                },
            )
        elif len(returns) == 1:
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": "shell",
                    "name": "shell_command",
                    "arguments": {
                        "command": "Set-Content shell-identity.txt shell-owned"
                    },
                },
            )
        else:
            self.completed_agents += 1
            decision = json.dumps(
                {"decision": "submit", "reason": "workspace inspected"}
            )
            yield StreamEvent(
                kind="text_delta",
                data={"delta": decision, "accumulated": decision},
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


def _runtime(
    tmp_path: Path, provider: object, workspace_for
) -> tuple[AgentRuntime, LocalArtifactStore]:
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    register_plan_agent(
        registry,
        provider=provider,
        artifacts=store,
        workspace_for=workspace_for,
        execution=ExecutionRuntime(project_root=tmp_path, environment_root=tmp_path),
    )
    runtime = AgentRuntime(
        type_registry=registry,
        project_root=tmp_path,
        rollout_dir=tmp_path / ".athena" / "logs" / "agents",
    )
    runtime.start()
    return runtime, store


@pytest.mark.asyncio
async def test_plan_agent_returns_structured_decision(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "h1"
    workspace.mkdir(parents=True)
    runtime, _store = _runtime(tmp_path, _ProseProvider(), lambda _id: workspace)

    _agent_id, run_id = await runtime.create_root(
        "plan", {"content": "Implement the hypothesis."}, agent_id="h1", name="h1"
    )
    summary = await runtime.wait_run(run_id, timeout=5)

    assert summary.status is RunStatus.FAILED
    assert summary.error is not None
    assert "structured output invalid" in summary.error
    await runtime.aclose()


@pytest.mark.asyncio
async def test_plan_agent_uses_its_named_workspace(tmp_path: Path) -> None:
    roots = {plan_id: tmp_path / "workspaces" / plan_id for plan_id in ("h1", "h2")}
    for root in roots.values():
        root.mkdir(parents=True)
    provider = _WorkspaceProvider()
    runtime, store = _runtime(tmp_path, provider, roots.__getitem__)

    for plan_id in ("h1", "h2"):
        agent_id, run_id = await runtime.create_root(
            "plan",
            {"content": "Inspect and implement."},
            agent_id=plan_id,
            name=plan_id,
        )
        summary = await runtime.wait_run(run_id, timeout=5)
        assert agent_id == plan_id
        assert summary.status is RunStatus.COMPLETED
        outer = json.loads(summary.response_ref or "{}")
        assert json.loads(await store.get_text(outer["result_ref"])) == {
            "decision": "submit",
            "reason": "workspace inspected",
            "suggestions": [],
        }

    assert (roots["h1"] / "identity.txt").read_text() == "owned"
    assert (roots["h2"] / "identity.txt").read_text() == "owned"
    assert (roots["h1"] / "shell-identity.txt").read_text(
        encoding="utf-8-sig"
    ).strip() == "shell-owned"
    assert (roots["h2"] / "shell-identity.txt").read_text(
        encoding="utf-8-sig"
    ).strip() == "shell-owned"
    assert provider.completed_agents == 2
    assert roots["h1"] != roots["h2"]
    await runtime.aclose()
