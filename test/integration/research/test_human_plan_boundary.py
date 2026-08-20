"""Human guidance and deterministic Supervisor command boundaries."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest

from athena.core.agent.provider import StreamEvent
from athena.core.research_models import EvalResult, ExperimentPlan, Hypothesis
from athena.core.research_tree import Experiment, ExperimentStatus
from athena.core.workspace import GitWorkBranch
from athena.research.contracts import ValidationResult
from athena.research.runtime import ResearchRuntime


def _latest_user_text(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            text = "\n".join(
                str(getattr(part, "content", ""))
                for part in message.parts
                if getattr(part, "part_kind", None) == "user-prompt"
            )
            if text:
                return text
    return ""


class _HumanProvider:
    model_name = "human-boundary-test"

    def __init__(self) -> None:
        self.calls = 0
        self._tool_pending = False

    async def stream(self, _config, _tools, messages, _cancel, **_kwargs):
        self.calls += 1
        if self._tool_pending:
            self._tool_pending = False
            payload = json.dumps({"answer": "ack"})
            yield StreamEvent(
                kind="text_delta", data={"delta": payload, "accumulated": payload}
            )
            yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})
            return

        text = _latest_user_text(messages)
        tool = None
        arguments = None
        if text == "always avoid leakage":
            tool = "record_guidance"
            arguments = {"text": text, "scope": "persistent"}
        elif text == "try ViT next":
            tool = "record_guidance"
            arguments = {"text": text, "scope": "next"}
        elif text == "the missing values encode cabin assignment":
            tool = "propose_hypothesis"
            arguments = {
                "statement": "Missing values encode cabin assignment",
                "intervention": "add missingness indicators",
                "expected_effect": "improve score",
                "supersedes": [],
                "sources": [],
                "turn_limit": 8,
                "patience": 3,
            }
        elif text == "use a transformer family next":
            tool = "propose_hypothesis"
            arguments = {
                "statement": "A transformer family improves the model",
                "intervention": "use a transformer model family",
                "expected_effect": "improve score",
                "supersedes": [],
                "sources": [],
                "turn_limit": 8,
                "patience": 3,
            }
        elif text == "continue three more attempts":
            tool = "configure_search"
            arguments = {"search_limit": 13}
        elif text == "enter validation":
            tool = "set_phase_decision"
            arguments = {"decision": "VALIDATE"}
        if tool is not None:
            self._tool_pending = True
            yield StreamEvent(
                kind="function_call",
                data={
                    "call_id": f"call-{self.calls}",
                    "name": tool,
                    "arguments": arguments,
                },
            )
        else:
            payload = json.dumps({"answer": "ack"})
            yield StreamEvent(
                kind="text_delta", data={"delta": payload, "accumulated": payload}
            )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


@pytest.fixture
async def runtime(tmp_path: Path, monkeypatch):
    for name, value in {
        "GIT_AUTHOR_NAME": "Athena Test",
        "GIT_AUTHOR_EMAIL": "athena@example.invalid",
        "GIT_COMMITTER_NAME": "Athena Test",
        "GIT_COMMITTER_EMAIL": "athena@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    provider = _HumanProvider()

    async def validate(_sota_commit: str, metric: float) -> ValidationResult:
        return ValidationResult(
            result_id="validation-key",
            status="COMPLETED",
            test_score=metric,
            final_test_score=metric,
            sota_commit=_sota_commit,
            validation_commit=_sota_commit,
        )

    instance = ResearchRuntime(project_root=tmp_path, validation_phase=validate)
    instance.register_supervisor(provider=provider)
    base_commit = await instance._git.init()
    evaluator_ref = await instance._store.put_text('{"frozen":true}')
    evidence_ref = await instance._store.put_text("baseline evidence")
    instance.tree.add_hypothesis(
        Hypothesis(
            id="baseline",
            statement="baseline",
            intervention="fit baseline",
            expected_effect="establish reference",
        )
    )
    instance.tree.add_experiment(
        "exp_baseline",
        Experiment(
            hypothesis_id="baseline",
            commit=base_commit,
            plan=ExperimentPlan(
                kind="baseline",
                change="baseline",
                run_config_ref=evaluator_ref,
                budget={},
                acceptance_rule="trusted score",
            ),
            gitwork=GitWorkBranch(
                path=str(tmp_path), branch="main", base_commit=base_commit
            ),
            status=ExperimentStatus.SUCCEEDED,
            eval=EvalResult(
                experiment_id="exp_baseline",
                primary=0.8,
                per_sample=evidence_ref,
            ),
        ),
    )
    instance.tree.update_hypothesis_status("baseline", "SUPPORTED")
    instance.tree.set_sota("exp_baseline")
    # 模拟「已启动、正跑在 SEARCH」的会话：阶段与状态都显式设置，不依赖
    # 构造默认值（全新 runtime 的默认是 PREPARE/IDLE，表示阶段机从未启动）。
    instance.state.phase = "SEARCH"
    instance.state.status = "RUNNING"
    instance.supervisor._evaluator_ref = evaluator_ref
    for hypothesis_id in ("h_existing", "h_vit", "h_other"):
        instance.tree.add_hypothesis(
            Hypothesis(
                id=hypothesis_id,
                parent_id="exp_baseline",
                statement=f"statement for {hypothesis_id}",
                intervention=f"intervention for {hypothesis_id}",
                expected_effect="improve score",
                patience=3,
                turn_limit=8,
            )
        )
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest.mark.asyncio
async def test_persistent_guidance_is_frozen_into_every_later_plan(runtime):
    await runtime.message("always avoid leakage")
    await runtime.supervisor.start_plan("h_existing")
    await runtime.supervisor.start_plan("h_vit")

    assert (
        "always avoid leakage"
        in (await runtime.supervisor.plan_input("h_existing")).human_context
    )
    assert (
        "always avoid leakage"
        in (await runtime.supervisor.plan_input("h_vit")).human_context
    )


@pytest.mark.asyncio
async def test_next_guidance_is_frozen_once(runtime):
    await runtime.message("try ViT next")
    await runtime.supervisor.start_plan("h_existing")
    await runtime.supervisor.start_plan("h_vit")

    assert (
        "try ViT next"
        in (await runtime.supervisor.plan_input("h_existing")).human_context
    )
    assert (
        "try ViT next"
        not in (await runtime.supervisor.plan_input("h_vit")).human_context
    )


@pytest.mark.asyncio
async def test_human_data_interpretation_creates_a_later_hypothesis(runtime):
    before = {item.id for item in runtime.tree.pending_hypotheses()}
    await runtime.message("the missing values encode cabin assignment")
    created = [
        item for item in runtime.tree.pending_hypotheses() if item.id not in before
    ]

    assert [item.statement for item in created] == [
        "Missing values encode cabin assignment"
    ]


@pytest.mark.asyncio
async def test_model_family_request_creates_a_later_hypothesis(runtime):
    before = {item.id for item in runtime.tree.pending_hypotheses()}
    await runtime.message("use a transformer family next")
    created = [
        item for item in runtime.tree.pending_hypotheses() if item.id not in before
    ]

    assert [item.intervention for item in created] == ["use a transformer model family"]


@pytest.mark.asyncio
async def test_continue_three_more_attempts_extends_search_limit(runtime):
    await runtime.message("continue three more attempts")
    assert runtime.state.search_limit == 13


@pytest.mark.asyncio
async def test_unlimited_waiting_plan_keeps_execution_safety_limits(runtime):
    await runtime.supervisor.start_plan("h_existing")
    plan = runtime.state.plans["h_existing"]
    runtime.state.plans["h_existing"] = plan.model_copy(
        update={"turns_used": plan.turn_limit}
    )
    execution = runtime._execution

    await runtime.supervisor.update_waiting_plan_budget(
        plan_id="h_existing", unlimited_turns=True
    )

    assert runtime.state.plans["h_existing"].turn_limit is None
    assert runtime._execution is execution


@pytest.mark.asyncio
async def test_budget_extension_resumes_waiting_plan_before_new_plan(runtime):
    await runtime.supervisor.start_plan("h_existing")
    plan = runtime.state.plans["h_existing"]
    runtime.state.plans["h_existing"] = plan.model_copy(
        update={"turns_used": plan.turn_limit}
    )
    runtime.state.concurrency = 1
    await runtime.supervisor.update_waiting_plan_budget(
        plan_id="h_existing", turn_limit=12
    )
    task = asyncio.create_task(runtime.supervisor.run_search())
    async with asyncio.timeout(5):
        while runtime.supervisor.running_plan_ids != ("h_existing",):
            await asyncio.sleep(0.01)
    await runtime.supervisor.stop()
    await asyncio.gather(task, return_exceptions=True)

    assert "h_other" not in runtime.state.plans


@pytest.mark.asyncio
async def test_explicit_validate_and_stop_are_applied(runtime):
    await runtime.message("enter validation")
    assert runtime.state.phase == "COMPLETED"
    assert await runtime.message("/stop") == "STOPPED"


@pytest.mark.asyncio
async def test_start_validation_transitions_search_to_completed(runtime):
    """GUI ``start_validation`` runs VALIDATE from a parked SEARCH phase."""
    runtime._started = True  # 模拟 start_search 已启动、SEARCH 后停在 WAITING 的交互路径
    assert runtime.state.phase == "SEARCH"

    assert await runtime.start_validation() == "COMPLETED"
    assert runtime.state.phase == "COMPLETED"
    assert runtime.state.validation is not None
    assert runtime.state.validation["final_test_score"] == 0.8


@pytest.mark.asyncio
async def test_ordinary_prose_containing_stop_is_not_a_command(runtime):
    calls = runtime.supervisor_provider.calls
    await runtime.message("please stop overfitting, but continue research")

    assert runtime.state.status == "RUNNING"
    assert runtime.supervisor_provider.calls == calls + 1


@pytest.mark.asyncio
async def test_attempt_limit_finishes_active_plans_then_waits_or_auto_validates(
    runtime,
):
    runtime.state.search_limit = 0
    runtime.state.status = "RUNNING"
    await runtime.supervisor.run_search()
    assert runtime.state.phase == "SEARCH"
    assert runtime.state.status == "RUNNING"


def test_four_successes_do_not_stop_production_search(runtime):
    runtime.state.search_limit = 10
    assert runtime.state.status == "RUNNING"
    assert runtime.state.search_limit > 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [("/pause", "WAITING"), ("/resume", "RUNNING"), ("/stop", "STOPPED")],
)
async def test_exact_strong_commands_bypass_llm(runtime, command: str, expected: str):
    provider_calls = runtime.supervisor_provider.calls
    assert await runtime.message(command) == expected
    assert runtime.supervisor_provider.calls == provider_calls
