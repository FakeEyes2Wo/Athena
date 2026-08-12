"""Long-lived SupervisorAgent construction and narrow decision tools."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.orchestration import RunToolProjector
from athena.agents.prompt_agent import load_prompt
from athena.core.agent.models import AgentConfig
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import ToolContext, ToolSpec

SUPERVISOR_AGENT_ID = "supervisor"
SUPERVISOR_AGENT_TYPE = "supervisor"
MAX_PLAN_TURNS = 200
MAX_PATIENCE = 5
MAX_CONCURRENCY = 4


class SupervisorActions(Protocol):
    """Deterministic actions implemented by the owning Supervisor runtime."""

    async def propose_hypothesis(self, **payload: object) -> dict[str, object]:
        """Register one validated research Hypothesis."""
        ...

    async def select_next_hypothesis(self, hypothesis_id: str) -> dict[str, object]:
        """Select one existing Hypothesis for the next Plan."""
        ...

    async def configure_search(self, **payload: object) -> dict[str, object]:
        """Update bounded Search configuration."""
        ...

    async def update_waiting_plan_budget(self, **payload: object) -> dict[str, object]:
        """Update one waiting Plan's execution budget."""
        ...

    async def set_phase_decision(self, decision: str) -> dict[str, object]:
        """Record the validated next research phase."""
        ...

    async def record_guidance(self, text: str, scope: str) -> dict[str, object]:
        """Persist Human guidance at the selected Plan boundary."""
        ...


class SupervisorAnswer(BaseModel):
    """Human-facing result of one serialized SupervisorAgent turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str = Field(min_length=1)


class _HypothesisProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    statement: str = Field(min_length=1)
    intervention: str = Field(min_length=1)
    expected_effect: str = Field(min_length=1)
    supersedes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    turn_limit: int = Field(ge=1, le=MAX_PLAN_TURNS)
    patience: int = Field(ge=1, le=MAX_PATIENCE)


class _NextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    hypothesis_id: str = Field(min_length=1)


class _SearchConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    search_limit: int | None = Field(default=None, ge=1)
    concurrency: int | None = Field(default=None, ge=1, le=MAX_CONCURRENCY)


class _WaitingPlanBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan_id: str = Field(min_length=1)
    turn_limit: int | None = Field(default=None, ge=1, le=MAX_PLAN_TURNS)
    unlimited_turns: bool = False
    patience: int | None = Field(default=None, ge=1, le=MAX_PATIENCE)


class _PhaseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["SEARCH", "VALIDATE"]


class _Guidance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1)
    scope: Literal["next", "persistent"]


class _ValidatedTool(BaseTool):
    """Validate model tool arguments before crossing the deterministic boundary."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_model: type[BaseModel],
        invoke: Callable[[BaseModel], Awaitable[dict[str, object]]],
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            description=description,
            input_schema=input_model.model_json_schema(),
        )
        self._input_model = input_model
        self._invoke = invoke

    async def execute(self, input: dict, ctx: ToolContext) -> dict[str, object]:
        """Validate one tool request before invoking its deterministic action."""
        del ctx
        request = self._input_model.model_validate(input)
        return await self._invoke(request)


class SupervisorToolProjector(RunToolProjector):
    """Project the Supervisor's deterministic tools into every Agent turn."""

    def __init__(self, actions: SupervisorActions) -> None:
        super().__init__(permissions={SUPERVISOR_AGENT_TYPE: set()})
        self._actions = actions

    def build(self, agent_type: str, session) -> ToolRegistry:
        """Build the Supervisor tool set for one Agent turn."""
        del session
        if agent_type != SUPERVISOR_AGENT_TYPE:
            raise ValueError(f"unsupported agent_type: {agent_type}")
        return supervisor_tool_registry(self._actions)


def supervisor_tool_registry(actions: SupervisorActions) -> ToolRegistry:
    """Build only the decision tools a SupervisorAgent is allowed to call."""

    registry = ToolRegistry()

    async def propose(value: BaseModel) -> dict[str, object]:
        """Forward a validated Hypothesis proposal."""
        return await actions.propose_hypothesis(**value.model_dump())

    async def select(value: BaseModel) -> dict[str, object]:
        """Forward a validated one-shot selection."""
        return await actions.select_next_hypothesis(value.hypothesis_id)  # type: ignore[attr-defined]

    async def configure(value: BaseModel) -> dict[str, object]:
        """Forward at least one validated Search setting."""
        payload = value.model_dump(exclude_none=True)
        if not payload:
            raise ValueError("at least one Search limit must be provided")
        return await actions.configure_search(**payload)

    async def update_budget(value: BaseModel) -> dict[str, object]:
        """Forward at least one validated waiting-Plan budget."""
        payload = value.model_dump(exclude_none=True)
        if payload.get("turn_limit") is not None and payload.get("unlimited_turns"):
            raise ValueError("turn_limit and unlimited_turns are mutually exclusive")
        if (
            set(payload) == {"plan_id", "unlimited_turns"}
            and not payload["unlimited_turns"]
        ):
            raise ValueError("at least one Plan budget must be provided")
        return await actions.update_waiting_plan_budget(**payload)

    async def decide_phase(value: BaseModel) -> dict[str, object]:
        """Forward the structured phase decision."""
        return await actions.set_phase_decision(value.decision)  # type: ignore[attr-defined]

    async def remember(value: BaseModel) -> dict[str, object]:
        """Forward scoped Human research guidance."""
        return await actions.record_guidance(value.text, value.scope)  # type: ignore[attr-defined]

    definitions = (
        (
            "propose_hypothesis",
            "Register one testable research hypothesis with bounded Plan budgets.",
            _HypothesisProposal,
            propose,
        ),
        (
            "select_next_hypothesis",
            "Select an existing Hypothesis once for the next new Plan.",
            _NextSelection,
            select,
        ),
        (
            "configure_search",
            "Change the Search attempt limit or concurrency.",
            _SearchConfiguration,
            configure,
        ),
        (
            "update_waiting_plan_budget",
            "Change the execution budget of one named waiting Plan.",
            _WaitingPlanBudget,
            update_budget,
        ),
        (
            "set_phase_decision",
            "Choose whether research remains in SEARCH or enters VALIDATE.",
            _PhaseDecision,
            decide_phase,
        ),
        (
            "record_guidance",
            "Record research guidance for the next Plan or all future Plans.",
            _Guidance,
            remember,
        ),
    )
    for name, description, input_model, invoke in definitions:
        registry.register(
            _ValidatedTool(
                name=name,
                description=description,
                input_model=input_model,
                invoke=invoke,
            )
        )
    return registry


def register_supervisor_agent(
    registry,
    *,
    provider: Any,
    artifacts,
    actions: SupervisorActions,
) -> None:
    """Register a fresh SupervisorAgent factory through AgentTypeRegistry."""

    def factory(_agent_id: str, _config: str | None = None) -> AgentSpec:
        """Construct one SupervisorAgent specification."""
        projector = SupervisorToolProjector(actions)
        tools = supervisor_tool_registry(actions)
        agent = Agent(
            provider,
            tools,
            load_prompt(SUPERVISOR_AGENT_TYPE),
            AgentConfig(name="supervisor-agent"),
            output_type=SupervisorAnswer,
            artifacts=artifacts,
        )
        return AgentSpec(
            runner=BaseAgentRunner(
                agent,
                tools=tools,
                projector=projector,
                agent_type=SUPERVISOR_AGENT_TYPE,
            ),
            codec=JsonCodec(),
        )

    registry.register(SUPERVISOR_AGENT_TYPE, factory)


__all__ = [
    "MAX_CONCURRENCY",
    "MAX_PATIENCE",
    "MAX_PLAN_TURNS",
    "SUPERVISOR_AGENT_ID",
    "SUPERVISOR_AGENT_TYPE",
    "SupervisorActions",
    "SupervisorAnswer",
    "SupervisorToolProjector",
    "register_supervisor_agent",
    "supervisor_tool_registry",
]
