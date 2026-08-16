"""Focused tests for GUI settings control over the Ideator mechanism."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from athena.core.agent.registry import AgentTypeRegistry
from athena.research.runtime import ResearchRuntime


def _runtime() -> ResearchRuntime:
    runtime = ResearchRuntime.__new__(ResearchRuntime)
    runtime._root = Path(".")
    runtime._model = None
    runtime._ideation = "ideageneration"
    runtime._direction = "maximize"
    runtime._tolerance = 0.0
    runtime._auto_validate = False
    runtime._registry = AgentTypeRegistry()
    runtime._supervisor = SimpleNamespace(
        state=SimpleNamespace(
            concurrency=1,
            search_limit=10,
            ideator_count=3,
            hypotheses_per_ideator=2,
            manual_mode=False,
            phase="PREPARE",
            status="RUNNING",
        )
    )
    return runtime


def test_settings_expose_the_current_ideation_mode() -> None:
    runtime = _runtime()

    snapshot = runtime.settings()

    assert snapshot["ideation"] == "ideageneration"


@pytest.mark.asyncio
async def test_apply_settings_switches_the_ideation_mechanism() -> None:
    runtime = _runtime()

    result = await runtime.apply_settings({"ideation": "debate"})

    assert runtime._ideation == "debate"
    assert result["ideation"] == "debate"


@pytest.mark.asyncio
async def test_apply_settings_rejects_unknown_ideation_values() -> None:
    runtime = _runtime()

    with pytest.raises(ValueError):
        await runtime.apply_settings({"ideation": "gated"})


@pytest.mark.asyncio
async def test_switching_ideation_unregisters_the_bound_ideator_contract() -> None:
    runtime = _runtime()
    runtime._registry.register("ideator", lambda agent_id, config=None: object())

    await runtime.apply_settings({"ideation": "baseline"})

    assert not runtime._registry.contains("ideator")
