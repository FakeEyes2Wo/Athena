"""Public dispatcher for research Agent turns."""

from pathlib import Path

from athena.agents.supervisor_agent import SUPERVISOR_AGENT_ID, SupervisorAnswer
from athena.agents.task_agents import DATA_AGENT_ID, register_data_agent
from athena.core.research_models import EdaResult
from athena.research.supervisor.experiment import load_agent_result
from athena.research.turns.common import wait_run_with_heartbeat
from athena.research.turns.general import GeneralTurnMixin
from athena.research.turns.ideator import IdeatorTurnMixin
from athena.research.turns.support import SupportVerificationMixin


class AgentTurnRunner(
    IdeatorTurnMixin,
    GeneralTurnMixin,
    SupportVerificationMixin,
):
    """Expose the runtime's Supervisor, Ideator, General, and Data turn commands."""

    async def run_supervisor_turn(self, text: str) -> str:
        """Run one serialized Supervisor turn and return its human-facing answer."""
        rt = self._runtime
        if rt.provider is None:
            raise RuntimeError("SupervisorAgent provider is not registered")
        request = {"content": text, "context_refs": []}
        if rt.agents.has_agent(SUPERVISOR_AGENT_ID):
            run_id = await rt.agents.followup(SUPERVISOR_AGENT_ID, request)
        else:
            _agent_id, run_id = await rt.agents.create_root(
                "supervisor",
                request,
                agent_id=SUPERVISOR_AGENT_ID,
                name=SUPERVISOR_AGENT_ID,
            )
        summary = await wait_run_with_heartbeat(
            rt,
            rt.agents,
            run_id,
            agent_id=SUPERVISOR_AGENT_ID,
            label="SupervisorAgent turn",
            plan=SUPERVISOR_AGENT_ID,
            project=False,
        )
        result = await load_agent_result(summary, rt.store, SupervisorAnswer)
        if result is None:
            raise RuntimeError(summary.error or "SupervisorAgent turn failed")
        await rt.publish_output(source="supervisor", channel="text", text=result.answer)
        return result.answer

    async def run_data_turn(self, request: str) -> None:
        """Run Data Agent analysis for an Ideator's EDA request."""
        rt = self._runtime
        if rt.provider is None:
            raise RuntimeError("Data Agent requires a registered Agent provider")
        eda_dir = self._resolve_eda_dir(rt)
        if not rt.registry.contains("data"):
            register_data_agent(
                rt.registry,
                provider=rt.provider,
                artifacts=rt.store,
                workspace=Path(eda_dir),
                runtime=rt.execution,
            )
        await rt.publish_output(
            source="agent",
            channel="text",
            text=f"补充 EDA 请求：{request}",
            plan=DATA_AGENT_ID,
        )
        task = {"content": request, "context_refs": []}
        if rt.agents.has_agent(DATA_AGENT_ID):
            run_id = await rt.agents.followup(DATA_AGENT_ID, task)
        else:
            _agent_id, run_id = await rt.agents.create_root(
                "data",
                task,
                agent_id=DATA_AGENT_ID,
                name=DATA_AGENT_ID,
            )
        summary = await wait_run_with_heartbeat(
            rt,
            rt.agents,
            run_id,
            agent_id=DATA_AGENT_ID,
            label="Data Agent turn",
            plan=DATA_AGENT_ID,
        )
        result = await load_agent_result(summary, rt.store, EdaResult)
        if result is None:
            raise RuntimeError(summary.error or "Data Agent turn failed")
        await rt.publish_output(
            source="agent",
            channel="text",
            text=f"补充 EDA 完成：{result.summary}",
            plan=DATA_AGENT_ID,
        )
