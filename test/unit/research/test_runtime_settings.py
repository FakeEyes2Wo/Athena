"""Focused tests for GUI settings control over the Ideator mechanism."""

import pytest

from athena.research.config import ResearchConfig, ResearchPaths, SearchLimits
from athena.research.runtime import ResearchRuntime
from athena.research.runtime.bootstrap import build_services
from athena.research.runtime.services import RuntimeOptions


def test_build_services_reads_search_limit_from_config(tmp_path) -> None:
    athena = tmp_path / ".athena"
    config = ResearchConfig(
        paths=ResearchPaths(
            root=tmp_path,
            athena=athena,
            workspaces=tmp_path / "workspaces",
        ),
        search=SearchLimits(search_limit=4),
    )

    services, _session = build_services(config, None)

    assert config.skip_validate is False
    assert services.durable.state.search_limit == 4


def test_skip_validate_defaults_to_false_in_runtime_options() -> None:
    assert RuntimeOptions().skip_validate is False


def test_research_runtime_composes_skip_validate(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path, skip_validate=True)

    assert runtime.config.skip_validate is True
    assert runtime.session.options.skip_validate is True
    assert runtime.supervisor._deps.phases.skip_validate is True
    assert runtime.settings()["skip_validate"] is True


def test_skip_validate_defaults_off_and_projects_to_settings(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    assert runtime.config.skip_validate is False
    assert runtime.session.options.skip_validate is False
    assert runtime.settings()["skip_validate"] is False


@pytest.mark.asyncio
async def test_apply_settings_updates_skip_validate_without_changing_auto_validate(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path, auto_validate=True, skip_validate=False
    )

    snapshot = await runtime.apply_settings({"skip_validate": True})

    assert snapshot["skip_validate"] is True
    assert runtime.session.options.skip_validate is True
    assert runtime.supervisor._deps.phases.skip_validate is True
    assert runtime.session.options.auto_validate is True
    assert runtime.supervisor._deps.phases.auto_validate is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1, "true", None, []])
async def test_apply_settings_rejects_non_boolean_skip_validate(
    tmp_path, value
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    with pytest.raises(ValueError, match="skip_validate must be a bool"):
        await runtime.apply_settings({"skip_validate": value})


def _runtime(tmp_path) -> ResearchRuntime:
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.state.status = "RUNNING"
    runtime.state.handoff_sources = ["kaggle", "literature"]
    return runtime


def test_settings_expose_the_current_ideation_mode(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    snapshot = runtime.settings()

    assert snapshot["ideation"] == "ideageneration"


@pytest.mark.asyncio
async def test_apply_settings_switches_the_ideation_mechanism(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    result = await runtime.apply_settings({"ideation": "debate"})

    assert runtime.ideation == "debate"
    assert result["ideation"] == "debate"


@pytest.mark.asyncio
async def test_apply_settings_rejects_unknown_ideation_values(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(ValueError):
        await runtime.apply_settings({"ideation": "gated"})


@pytest.mark.asyncio
async def test_apply_settings_handoff_sources_persists(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    result = await runtime.apply_settings({"handoff_sources": ["kaggle", "literature"]})

    assert runtime.state.handoff_sources == ["kaggle", "literature"]
    assert result["handoff_sources"] == ["kaggle", "literature"]


@pytest.mark.asyncio
async def test_apply_settings_rejects_invalid_handoff_sources(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(ValueError):
        await runtime.apply_settings({"handoff_sources": ["kaggle", "web"]})


@pytest.mark.asyncio
async def test_switching_ideation_unregisters_the_bound_ideator_contract(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.registry.register("ideator", lambda agent_id, config=None: object())

    await runtime.apply_settings({"ideation": "baseline"})

    assert not runtime.registry.contains("ideator")


@pytest.mark.asyncio
async def test_start_task_rearms_after_a_terminal_failure(tmp_path) -> None:
    """web 端 FAILED 后再次 start 应重新武装 runtime，而不是直接返回 FAILED。"""
    runtime = _runtime(tmp_path)
    runtime.session.lifecycle.started = True
    runtime.session.lifecycle.task = None
    runtime.state.status = "FAILED"
    # An existing confirmed checkpoint resumes without a new gate.
    runtime.state.task_understanding = {"title": "retry after rules accepted"}
    runtime.state.task_text = "retry after rules accepted"
    calls: list[str] = []

    async def fake_start() -> None:
        calls.append("start")
        runtime.state.status = "RUNNING"

    runtime.start = fake_start  # type: ignore[method-assign]

    result = await runtime.start_task("retry after rules accepted")

    assert calls == ["start"]
    assert result == "RUNNING"
    assert runtime.task_text == "retry after rules accepted"
