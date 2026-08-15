"""Long-lived SupervisorAgent construction and narrow decision tools."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.base_runner import BaseAgentRunner
from athena.agents.orchestration import RunToolProjector
from athena.agents.prompt_agent import load_prompt
from athena.core.agent.models import AgentConfig
from athena.core.agent.runtime import Agent
from athena.core.agent.tools.user_input import RequestUserInputTool
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.tool import BaseTool, ToolRegistry
from athena.core.tool_types import AskUser, ToolContext, ToolSpec

SUPERVISOR_AGENT_ID = "supervisor"
SUPERVISOR_AGENT_TYPE = "supervisor"
MAX_PLAN_TURNS = 12
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

    async def set_manual_mode(self, manual: bool) -> dict[str, object]:
        """Toggle SEARCH scheduling between auto and manual hypothesis selection."""
        ...

    async def set_kaggle_enabled(
        self, enabled: bool, download: bool = True
    ) -> dict[str, object]:
        """Enable Kaggle tools and set whether to download data locally."""
        ...

    async def record_task_understanding(self, **payload: object) -> dict[str, object]:
        """Persist the structured task understanding for the GUI intent preview."""
        ...

    async def read_hypotheses(self) -> dict[str, object]:
        """Read-only snapshot of pending hypotheses and current SOTA."""
        ...

    async def record_guidance(self, text: str, scope: str) -> dict[str, object]:
        """Persist Human guidance at the selected Plan boundary."""
        ...

    async def read_state(self) -> dict[str, object]:
        """Read-only snapshot of the current research phase and configuration."""
        ...

    async def read_plans(self) -> dict[str, object]:
        """Read-only snapshot of running and waiting Plans."""
        ...

    async def dispatch_general(self, task: str) -> dict[str, object]:
        """Dispatch one General Agent to do concrete work and return its result."""
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


class _ManualMode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    manual: bool


class _KaggleConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
    download: bool = True


class _TaskUnderstanding(BaseModel):
    """Structured task understanding recorded on the first task-understanding turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = ""
    dataset: str = ""
    target: str = ""
    task_type: str = "other"
    primary_metric: str = "accuracy"
    direction: Literal["maximize", "minimize"] = "maximize"
    evaluation_plan: str = ""


class _Guidance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1)
    scope: Literal["next", "persistent"]


class _NoInput(BaseModel):
    """Empty input for read-only Supervisor tools."""

    model_config = ConfigDict(extra="forbid", strict=True)


class _GeneralTask(BaseModel):
    """Task description for dispatching one General Agent."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task: str = Field(min_length=1)


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

    async def set_manual(value: BaseModel) -> dict[str, object]:
        """Forward the auto/manual mode toggle."""
        return await actions.set_manual_mode(value.manual)  # type: ignore[attr-defined]

    async def set_kaggle(value: BaseModel) -> dict[str, object]:
        """Forward the Kaggle tool attachment + download decision."""
        return await actions.set_kaggle_enabled(  # type: ignore[attr-defined]
            value.enabled, value.download
        )

    async def record_understanding(value: BaseModel) -> dict[str, object]:
        """Forward the structured task understanding."""
        return await actions.record_task_understanding(**value.model_dump())

    async def read_hyps(value: BaseModel) -> dict[str, object]:
        """Forward the read-only hypothesis snapshot."""
        return await actions.read_hypotheses()

    async def remember(value: BaseModel) -> dict[str, object]:
        """Forward scoped Human research guidance."""
        return await actions.record_guidance(value.text, value.scope)  # type: ignore[attr-defined]

    async def read_state(value: BaseModel) -> dict[str, object]:
        """Forward the current phase/status/Search read-only snapshot."""
        return await actions.read_state()

    async def read_plans(value: BaseModel) -> dict[str, object]:
        """Forward the running/waiting Plan read-only snapshot."""
        return await actions.read_plans()

    async def dispatch(value: BaseModel) -> dict[str, object]:
        """Forward a General Agent dispatch task."""
        return await actions.dispatch_general(value.task)  # type: ignore[attr-defined]

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
            "set_manual_mode",
            "Toggle SEARCH between auto (priority queue) and manual hypothesis selection.",
            _ManualMode,
            set_manual,
        ),
        (
            "configure_kaggle",
            "Enable or disable Kaggle tools for this run, and choose whether to download "
            "the competition dataset locally. Enable when the task is a Kaggle competition.",
            _KaggleConfiguration,
            set_kaggle,
        ),
        (
            "record_task_understanding",
            "Record the structured task understanding (title, dataset, target, task type, "
            "primary metric + direction, evaluation plan) for the GUI intent preview. "
            "Call this on the first task-understanding turn.",
            _TaskUnderstanding,
            record_understanding,
        ),
        (
            "read_hypotheses",
            "Read the pending hypotheses, their priorities and the current SOTA.",
            _NoInput,
            read_hyps,
        ),
        (
            "record_guidance",
            "Record research guidance for the next Plan or all future Plans.",
            _Guidance,
            remember,
        ),
        (
            "read_state",
            "Read the current research phase, status, and Search limits.",
            _NoInput,
            read_state,
        ),
        (
            "read_plans",
            "Read running and waiting Plan budgets and turn usage.",
            _NoInput,
            read_plans,
        ),
        (
            "dispatch_general",
            "Dispatch one General Agent to inspect, run, or fix something and report back.",
            _GeneralTask,
            dispatch,
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
    # Supervisor 是唯一人类交互出口：同步向人类提问（依赖注入的 ask_user）。
    registry.register(RequestUserInputTool())
    return registry


def register_supervisor_agent(
    registry,
    *,
    provider: Any,
    artifacts,
    actions: SupervisorActions,
    ask_user: Any = None,
) -> None:
    """Register a fresh SupervisorAgent factory through AgentTypeRegistry.

    ``ask_user`` 是 ``(thread, turn) -> (prompt) -> 回答`` 的工厂，供
    ``request_user_input`` 工具阻塞等待人类回答；未提供则该工具报错。
    """

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
                ask_user=ask_user,
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
