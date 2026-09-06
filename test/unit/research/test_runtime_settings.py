"""Focused tests for GUI settings control over the Ideator mechanism."""

import os
from dataclasses import fields
from inspect import signature

import pytest

from athena.research.config import (
    ResearchConfig,
    ResearchOptions,
    ResearchPaths,
    ResearchPolicy,
    RuntimeDependencies,
    SearchLimits,
    SessionConfig,
)
from athena.research.runtime import ResearchRuntime
from athena.research.runtime.bootstrap import build_services
from athena.research.runtime.services import (
    ComputeSession,
    ExperimentServices,
    LifecycleSession,
    ResearchInfrastructure,
    ResearchServices,
    ResearchSession,
    ResearchWorkflow,
    RuntimeOptions,
    SurveySession,
)
from athena.research.runtime.settings import _apply_model_connection

SERVICE_RECORDS = (
    ResearchInfrastructure,
    ExperimentServices,
    ResearchWorkflow,
    ResearchServices,
    LifecycleSession,
    SurveySession,
    ComputeSession,
    RuntimeOptions,
    ResearchSession,
)


def test_runtime_configuration_surface_stays_grouped() -> None:
    assert tuple(signature(ResearchRuntime).parameters) == (
        "project_root",
        "session",
        "research",
        "dependencies",
    )
    assert tuple(field.name for field in fields(ResearchConfig)) == (
        "paths",
        "session_id",
        "research",
        "dependencies",
    )
    assert (
        max(
            len(fields(group))
            for group in (SessionConfig, ResearchOptions, RuntimeDependencies)
        )
        == 6
    )
    assert not hasattr(ResearchOptions(), "__dict__")


def test_runtime_service_records_stay_small_and_slotted() -> None:
    assert max(len(fields(record)) for record in SERVICE_RECORDS) == 5
    assert all("__slots__" in record.__dict__ for record in SERVICE_RECORDS)


def test_runtime_does_not_retain_a_settings_proxy(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    assert set(runtime.__dict__) == {"_config", "_services", "_session"}


def test_build_services_reads_search_limit_from_config(tmp_path) -> None:
    athena = tmp_path / ".athena"
    config = ResearchConfig(
        paths=ResearchPaths(
            root=tmp_path,
            athena=athena,
            workspaces=tmp_path / "workspaces",
        ),
        research=ResearchOptions(search=SearchLimits(search_limit=4)),
    )

    services, _session = build_services(config, None)

    assert config.research.policy.skip_validate is False
    assert services.state.search_limit == 4


def test_skip_validate_defaults_to_false_in_runtime_options() -> None:
    assert RuntimeOptions().skip_validate is False


def test_research_runtime_composes_skip_validate(tmp_path) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path,
        research=ResearchOptions(policy=ResearchPolicy(skip_validate=True)),
    )

    assert runtime.config.research.policy.skip_validate is True
    assert runtime.session.options.skip_validate is True
    assert runtime.supervisor._deps.phases.validation_mode == "skip"
    assert runtime.settings()["skip_validate"] is True


def test_skip_validate_defaults_off_and_projects_to_settings(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)

    assert runtime.config.research.policy.skip_validate is False
    assert runtime.session.options.skip_validate is False
    assert runtime.settings()["skip_validate"] is False


def test_settings_expose_safe_authority_mode_only(tmp_path) -> None:
    from athena.research.prepare.authority_local import LocalBaselineAuthorityStore

    runtime = ResearchRuntime(
        project_root=tmp_path,
        dependencies=RuntimeDependencies(
            baseline_authority=LocalBaselineAuthorityStore(
                tmp_path / "controller", tmp_path, "default"
            ),
        ),
    )

    snapshot = runtime.settings()

    assert snapshot["authority_mode"] == "local"
    assert snapshot["authority_security_level"] == "same-user local filesystem"
    assert "authority_root" not in snapshot


@pytest.mark.asyncio
async def test_apply_settings_updates_skip_validate_without_changing_auto_validate(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(
        project_root=tmp_path,
        research=ResearchOptions(policy=ResearchPolicy(auto_validate=True)),
    )

    snapshot = await runtime.apply_settings({"skip_validate": True})

    assert snapshot["skip_validate"] is True
    assert runtime.session.options.skip_validate is True
    assert runtime.supervisor._deps.phases.validation_mode == "skip"
    assert runtime.session.options.auto_validate is True


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


def test_masked_model_key_is_not_written_but_new_key_can_be_updated(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-real-key")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    _apply_model_connection(
        {"model_connection": {"provider": "deepseek", "llm_api_key": "sec********alue"}}
    )
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "LLM_PROVIDER=deepseek\n"
    assert os.environ.get("DEEPSEEK_API_KEY") == "deepseek-real-key"
    assert "LLM_API_KEY" not in os.environ

    _apply_model_connection(
        {"model_connection": {"provider": "deepseek", "llm_api_key": "new-secret"}}
    )
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "LLM_PROVIDER=deepseek\nLLM_API_KEY=new-secret\n"
    )
    assert os.environ["LLM_API_KEY"] == "new-secret"


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
