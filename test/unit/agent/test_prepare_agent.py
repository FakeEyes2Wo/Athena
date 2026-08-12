"""PrepareAgent registration and workspace-tool contract."""

import json
from pathlib import Path

import pytest

from athena.agents.prepare_agent import register_prepare_agent
from athena.core.agent.agent_runtime import AgentRuntime
from athena.core.agent.provider import StreamEvent
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.types import RunStatus
from athena.core.artifact_store import LocalArtifactStore
from athena.execution.runtime import ExecutionRuntime


class _WriteManifestProvider:
    model_name = "prepare-test"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": "write-1",
                    "name": "write_file",
                    "arguments": {
                        "path": "experiment.json",
                        "content": json.dumps(
                            {
                                "version": 1,
                                "commands": [["python", "model.py"]],
                                "outputs": {
                                    "predictions": "outputs/predictions.csv",
                                    "report": "outputs/report.md",
                                    "evaluator": "evaluator/eval.py",
                                },
                            }
                        ),
                    },
                },
            )
        else:
            answer = json.dumps(
                {"decision": "submit", "reason": "baseline ready", "suggestions": []}
            )
            yield StreamEvent(
                kind="text_delta", data={"delta": answer, "accumulated": answer}
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


@pytest.mark.asyncio
async def test_prepare_uses_registered_workspace_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "prepare"
    workspace.mkdir()
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    provider = _WriteManifestProvider()
    registry = AgentTypeRegistry()
    register_prepare_agent(
        registry,
        provider=provider,
        artifacts=artifacts,
        workspace=workspace,
        runtime=ExecutionRuntime(project_root=workspace, store=artifacts),
    )
    agent_runtime = AgentRuntime(
        type_registry=registry,
        project_root=tmp_path,
        rollout_dir=tmp_path / ".athena" / "logs" / "agents",
    )
    agent_runtime.start()
    try:
        agent_id, run_id = await agent_runtime.create_root(
            "prepare",
            {"content": "inspect data and create experiment.json"},
            agent_id="prepare",
            name="prepare",
        )
        summary = await agent_runtime.wait_run(run_id, timeout=5)

        assert agent_id == "prepare"
        assert summary.status is RunStatus.COMPLETED
        assert (workspace / "experiment.json").is_file()
    finally:
        await agent_runtime.aclose()
