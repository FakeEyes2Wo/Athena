import json

import pytest

from athena.agents.task_agents import register_validate_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import RunStatus
from athena.core.artifact_store import LocalArtifactStore
from athena.execution.runtime import ExecutionRuntime


class _ExplanationProvider:
    model_name = "validate-test"

    async def stream(self, _config, tools, _messages, _cancel, **_kwargs):
        assert {spec.name for spec in tools.specs} >= {"read_file", "write_file"}
        payload = '{"explanation":"Repair the runtime-only device selection."}'
        yield StreamEvent(
            kind="text_delta", data={"delta": payload, "accumulated": payload}
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


@pytest.mark.asyncio
async def test_validate_agent_registers_with_stable_identity_and_structured_output(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = LocalArtifactStore(tmp_path / "artifacts")
    registry = AgentTypeRegistry()
    register_validate_agent(
        registry,
        provider=_ExplanationProvider(),
        artifacts=store,
        workspace=workspace,
        runtime=ExecutionRuntime(project_root=workspace, store=store),
    )
    agents = AgentRuntime(
        type_registry=registry,
        project_root=tmp_path,
        rollout_dir=tmp_path / ".athena" / "logs" / "agents",
    )
    agents.start()

    agent_id, run_id = await agents.create_root(
        "validate",
        {"content": "Repair validation execution without changing semantics."},
        name="validate",
        agent_id="validate",
    )
    summary = await agents.wait_run(run_id, timeout=5)

    assert agent_id == "validate"
    assert summary.status is RunStatus.COMPLETED
    outer = json.loads(summary.response_ref)
    result = json.loads(await store.get_text(outer["result_ref"]))
    assert result == {"explanation": "Repair the runtime-only device selection."}
    assert (tmp_path / ".athena" / "logs" / "agents" / "validate.jsonl").is_file()
    await agents.aclose()
