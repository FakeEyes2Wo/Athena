"""Fail-closed authority preflight with a tiny, capability-scoped repair agent.

This module deliberately does not use the normal General Agent registration:
that registration includes generic file tools and (when configured) shell.  A
repair action is an explicitly injected host callback with no arguments.  The
agent receives only those callbacks and a safe failure category.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from athena.agents.base_runner import BaseAgentRunner
from athena.core.agent.models import AgentConfig
from athena.core.agent.registry import AgentTypeRegistry
from athena.core.agent.runtime import Agent
from athena.core.agent.types import AgentSpec, JsonCodec
from athena.core.tool import ToolRegistry, tool
from athena.research.prepare.authority import BaselineAuthorityError

REPAIR_AGENT_TYPE = "environment_repair"
REPAIR_AGENT_ID = "environment-repair"
MAX_REPAIR_ATTEMPTS = 2
REPAIR_TIMEOUT_SECONDS = 90.0
AUTHORITY_TIMEOUT_SECONDS = 10.0
REPAIR_CLEANUP_TIMEOUT_SECONDS = 5.0
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
logger = logging.getLogger(__name__)

RepairAction = Callable[[], Awaitable[None]]


class EnvironmentRepairResult(BaseModel):
    """Minimal structured response expected from the repair worker."""

    model_config = ConfigDict(extra="forbid", strict=True)

    result: str = Field(min_length=1)


def _validate_actions(
    actions: Mapping[str, RepairAction] | None,
) -> dict[str, RepairAction]:
    if actions is None:
        return {}
    if not isinstance(actions, Mapping):
        raise BaselineAuthorityError("environment repair actions are invalid")
    if len(actions) > 8:
        raise BaselineAuthorityError("too many environment repair actions")
    validated: dict[str, RepairAction] = {}
    for name, callback in actions.items():
        if not isinstance(name, str) or _ACTION_RE.fullmatch(name) is None:
            raise BaselineAuthorityError("environment repair action name is invalid")
        if not callable(callback):
            raise BaselineAuthorityError("environment repair action is not callable")
        validated[name] = callback
    return validated


def _callback_tools(actions: Mapping[str, RepairAction]) -> ToolRegistry:
    """Build one operation tool over the explicitly injected callbacks."""
    registry = ToolRegistry()

    used = False

    @tool(
        name="perform_operation",
        description="Run one controller-approved environment repair operation.",
    )
    async def perform_operation(operation: str) -> dict[str, str]:
        """Invoke at most one controller-registered repair callback."""
        nonlocal used
        if used:
            return {"status": "already_used"}
        used = True
        callback = actions.get(operation)
        if callback is None:
            return {"status": "not_allowlisted"}
        try:
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - callback details stay off-model
            return {"status": "failed"}
        return {"status": "completed"}

    registry.register(perform_operation)
    return registry


def _repair_prompt(actions: Mapping[str, RepairAction]) -> str:
    names = ", ".join(actions) or "(none)"
    return (
        "You are the bounded environment repair agent. The platform preflight "
        "reported only the safe failure kind `transient_authority_unavailable`; "
        "exception text, paths, credentials, and secrets are intentionally hidden. "
        "Use only an explicitly listed repair tool, at most as needed, then return "
        "a short JSON object with a result string. Never read files, run shell, "
        "guess credentials, or claim that authority is healthy.\n\n"
        f"Call perform_operation at most once with one of these operation names: {names}"
    )


def _register_repair_agent(runtime: Any, actions: Mapping[str, RepairAction]) -> None:
    if runtime.provider is None:
        raise BaselineAuthorityError("environment repair requires a provider")
    registry: AgentTypeRegistry = runtime.registry
    if registry.contains(REPAIR_AGENT_TYPE):
        registry.unregister(REPAIR_AGENT_TYPE)
    tools = _callback_tools(actions)
    prompt = _repair_prompt(actions)

    def factory(_agent_id: str) -> AgentSpec:
        """Build a repair worker with no generic workspace tools."""
        agent = Agent(
            runtime.provider,
            tools,
            prompt,
            AgentConfig(max_turns=6, name=REPAIR_AGENT_ID),
            output_type=EnvironmentRepairResult,
            artifacts=runtime.store,
        )
        return AgentSpec(
            runner=BaseAgentRunner(agent, tools=tools, agent_type=REPAIR_AGENT_TYPE),
            codec=JsonCodec(),
        )

    registry.register(REPAIR_AGENT_TYPE, factory)


async def _run_repair_agent(runtime: Any, actions: Mapping[str, RepairAction]) -> None:
    _register_repair_agent(runtime, actions)
    agents = runtime.agents
    run_id: str | None = None
    try:
        start = getattr(agents, "start", None)
        if callable(start):
            start()
        _, run_id = await agents.create_root(
            REPAIR_AGENT_TYPE,
            {
                "content": (
                    "Repair the transient authority connection using the approved "
                    "controller action tools, then report the outcome."
                ),
                "context_refs": [],
            },
            agent_id=REPAIR_AGENT_ID,
            name=REPAIR_AGENT_ID,
        )
        await asyncio.wait_for(agents.wait_run(run_id), timeout=REPAIR_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        if run_id is not None:
            await _interrupt_and_reap(agents, run_id)
        raise
    except TimeoutError as exc:
        if run_id is not None:
            await _interrupt_and_reap(agents, run_id)
        raise BaselineAuthorityError("environment repair timed out") from exc
    finally:
        if run_id is not None:
            await _reap(agents)


async def _interrupt_and_reap(agents: Any, run_id: str) -> None:
    try:
        await asyncio.wait_for(
            agents.interrupt(REPAIR_AGENT_ID, "environment_repair_cancelled"),
            timeout=REPAIR_CLEANUP_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask cancellation
        logger.debug("repair interrupt cleanup failed: %s", type(exc).__name__)
    await _reap(agents)


async def _reap(agents: Any) -> None:
    try:
        await asyncio.wait_for(
            agents.reap(REPAIR_AGENT_ID, recursive=True),
            timeout=REPAIR_CLEANUP_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask repair result
        logger.debug("repair reap cleanup failed: %s", type(exc).__name__)


async def _audit(runtime: Any, attempt: int, status: str) -> None:
    publish = getattr(runtime, "publish_output", None)
    if publish is None:
        return
    await publish(
        source="supervisor",
        channel="text",
        text=(
            f"Environment authority preflight: repair attempt {attempt}/"
            f"{MAX_REPAIR_ATTEMPTS}, status={status}."
        ),
        plan=REPAIR_AGENT_TYPE,
    )


async def preflight(
    runtime: Any,
    repair_actions: Mapping[str, RepairAction] | None = None,
) -> None:
    """Verify authority before paid research, repairing transient outages only."""
    authority = getattr(runtime, "baseline_authority", None)
    if authority is None:
        raise BaselineAuthorityError(
            "external baseline authority capability is required before research"
        )
    actions = _validate_actions(repair_actions)
    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        try:
            await asyncio.wait_for(authority.load(), timeout=AUTHORITY_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ConnectionError):
            if attempt >= MAX_REPAIR_ATTEMPTS:
                await _audit(runtime, attempt, "unavailable")
                raise BaselineAuthorityError(
                    "baseline authority unavailable after bounded repair"
                ) from None
            if runtime.provider is None or not actions:
                await _audit(runtime, attempt, "repair_not_configured")
                raise BaselineAuthorityError(
                    "baseline authority unavailable and repair is not configured"
                ) from None
            await _audit(runtime, attempt + 1, "repairing")
            try:
                await _run_repair_agent(runtime, actions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await _audit(runtime, attempt + 1, "repair_failed")
                raise BaselineAuthorityError("environment repair failed") from exc
            # The next loop iteration is the mandatory platform recheck.
        except BaselineAuthorityError:
            await _audit(runtime, attempt, "nonrepairable")
            raise BaselineAuthorityError(
                "baseline authority preflight failed: nonrepairable"
            ) from None
        except Exception:  # noqa: BLE001 - hide untrusted authority details from UI
            await _audit(runtime, attempt, "nonrepairable")
            raise BaselineAuthorityError(
                "baseline authority preflight failed: nonrepairable"
            ) from None
        else:
            await _audit(runtime, attempt, "ready")
            return


__all__ = [
    "MAX_REPAIR_ATTEMPTS",
    "REPAIR_AGENT_ID",
    "REPAIR_AGENT_TYPE",
    "EnvironmentRepairResult",
    "preflight",
]
