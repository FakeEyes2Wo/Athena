"""Production ResearchRuntime clarification RPC wiring tests.

These tests use a real ``ResearchRuntime`` (not a fake) with a stub broker to
ensure the GUI-facing RPC methods are actually present and work end-to-end at
the runtime boundary.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage

from athena.core.agent import AgentConfig, BaseProvider
from athena.core.agent.provider import StreamEvent
from athena.core.human_request import HumanOutcome
from athena.core.tool import ToolRegistry
from athena.research import ResearchRuntime
from athena.research.clarification.controller import CLARIFICATION_FAILURE_NOTICE
from athena.research.clarification.generator import (
    DeterministicClarificationGenerator,
)
from athena.research.clarification.llm_generator import (
    REPORT_TASK_UNDERSTANDING_TOOL,
    TASK_UNDERSTANDING_SCOPE,
    LLMClarificationGenerator,
)
from athena.research.config import (
    ProviderConfig,
    ResearchConfig,
    ResearchOptions,
    RuntimeDependencies,
    SessionConfig,
    TaskConfig,
)
from athena.research.runtime import facade as runtime_facade
from athena.research.runtime.bootstrap import build_paths, build_services


class StubBroker:
    def __init__(self) -> None:
        self.asked: list[object] = []

    async def ask(self, request):
        self.asked.append(request)
        return HumanOutcome(
            request_id=request.request_id,
            kind="text",
            value="answer",
            settled_at=datetime.now(UTC),
        )

    async def cancel_scope(self, session_id: str, scope_id: str):
        return []


def _final_envelope(summary: str) -> str:
    return json.dumps(
        {
            "public_update": {"stage": "synthesis", "summary": summary},
            "step": {
                "kind": "final",
                "understanding": {
                    "title": "Churn",
                    "task_type": "classification",
                },
                "unresolved": [],
            },
        }
    )


class ScriptedProvider(BaseProvider):
    def __init__(
        self, *, envelope: str | None = None, error: str | None = None
    ) -> None:
        self.envelope = envelope
        self.error = error
        self.calls = 0
        self.tool_names: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return "scripted"

    @property
    def client(self) -> Any:
        return object()

    async def stream(
        self,
        config: AgentConfig,
        tools: ToolRegistry,
        messages: list[ModelMessage],
        cancel: asyncio.Event,
        *,
        output_type: type | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        del config, messages, cancel, output_type
        self.calls += 1
        self.tool_names.append([spec.name for spec in tools.specs])
        if self.error is not None:
            raise RuntimeError(self.error)
        if self.calls == 1:
            yield StreamEvent(
                "function_call",
                {
                    "call_id": "progress-1",
                    "name": REPORT_TASK_UNDERSTANDING_TOOL,
                    "arguments": {
                        "stage": "analysis",
                        "summary": "Checking target; api_key=sk-secret-value",
                    },
                },
            )
        else:
            assert self.envelope is not None
            yield StreamEvent(
                "text_delta",
                {"delta": self.envelope, "accumulated": self.envelope},
            )
        yield StreamEvent("response_completed")


class ProviderFactory:
    def __init__(self, provider: BaseProvider) -> None:
        self.provider = provider
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, model: str, client: Any = None) -> BaseProvider:
        self.calls.append((model, client))
        return self.provider


class ResumingClarificationController:
    """Small controller double that exposes the retry/revise async boundary."""

    def __init__(self) -> None:
        self.run_inputs: list[object] = []

    def retry(self, draft_id: str, revision: int) -> object:
        return ("retry", draft_id, revision)

    def revise(self, draft_id: str, revision: int, instruction: str) -> object:
        return ("revise", draft_id, revision, instruction)

    async def run(self, draft: object) -> object:
        self.run_inputs.append(draft)
        return {"status": "READY_FOR_CONFIRMATION", "draft": draft}


def _install_provider(monkeypatch, provider: BaseProvider) -> ProviderFactory:
    factory = ProviderFactory(provider)
    monkeypatch.setattr(runtime_facade, "ResponsesProvider", factory)
    return factory


_NO_PROVIDER = ProviderConfig()


def _runtime(
    root: Path, broker: StubBroker, provider: ProviderConfig = _NO_PROVIDER
) -> ResearchRuntime:
    return ResearchRuntime(
        project_root=root,
        session=SessionConfig(session_id="s1"),
        research=ResearchOptions(task=TaskConfig(confirmation_gate=True)),
        dependencies=RuntimeDependencies(provider=provider, broker=broker),
    )


@pytest.mark.asyncio
async def test_real_runtime_exposes_clarification_rpc(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, StubBroker())
    try:
        draft = await runtime.task_clarification_start("predict churn")
        assert draft.status == "READY_FOR_CONFIRMATION"
        assert draft.questions_asked == 4

        got = await runtime.task_clarification_get(draft.draft_id)
        assert got.draft_id == draft.draft_id
        assert got.revision == draft.revision

        cancelled = await runtime.task_clarification_cancel(
            draft.draft_id, draft.revision
        )
        assert cancelled.status == "CANCELLED"

        # Cancellation clears the previous draft so a different task can start.
        again = await runtime.task_clarification_start("another task")
        assert again.status == "READY_FOR_CONFIRMATION"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_retry_and_revise_resume_clarification_generation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, StubBroker())
    controller = ResumingClarificationController()
    runtime.services.workflow.clarification = controller
    try:
        retried = await runtime.task_clarification_retry("draft-1", 2)
        revised = await runtime.task_clarification_revise("draft-1", 3, "add recall")
    finally:
        await runtime.aclose()

    assert retried["status"] == "READY_FOR_CONFIRMATION"
    assert revised["status"] == "READY_FOR_CONFIRMATION"
    assert controller.run_inputs == [
        ("retry", "draft-1", 2),
        ("revise", "draft-1", 3, "add recall"),
    ]


def test_configured_build_services_requires_explicit_provider(tmp_path: Path) -> None:
    config = ResearchConfig(
        paths=build_paths(tmp_path, None),
        dependencies=RuntimeDependencies(provider=ProviderConfig(model="configured")),
    )

    with pytest.raises(ValueError, match="provider"):
        build_services(config, StubBroker(), provider=None)


@pytest.mark.asyncio
async def test_configured_runtime_constructs_and_shares_one_provider(
    tmp_path: Path, monkeypatch
) -> None:
    provider = ScriptedProvider(envelope=_final_envelope("ready"))
    factory = _install_provider(monkeypatch, provider)
    client = object()
    runtime = _runtime(
        tmp_path,
        StubBroker(),
        ProviderConfig(model="fake-model", client=client),
    )
    try:
        controller = runtime.services.workflow.clarification
        assert controller is not None
        assert factory.calls == [("fake-model", client)]
        assert runtime.provider is provider
        assert isinstance(controller._generator, LLMClarificationGenerator)
        assert controller._generator._provider is provider
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_providerless_draft_stays_schema_v1_and_is_readable_with_provider(
    tmp_path: Path, monkeypatch
) -> None:
    providerless = _runtime(tmp_path, StubBroker())
    try:
        controller = providerless.services.workflow.clarification
        assert controller is not None
        assert isinstance(controller._generator, DeterministicClarificationGenerator)
        draft = await providerless.task_clarification_start("predict churn")
        assert "generation_mode" not in draft.model_dump()
    finally:
        await providerless.aclose()

    _install_provider(monkeypatch, ScriptedProvider(envelope=_final_envelope("unused")))
    configured = _runtime(tmp_path, StubBroker(), ProviderConfig(model="fake-model"))
    try:
        restored = await configured.task_clarification_get(draft.draft_id)
        assert restored.schema_version == 1
        assert "generation_mode" not in restored.model_dump()
    finally:
        await configured.aclose()


@pytest.mark.asyncio
async def test_llm_runtime_publishes_scoped_redacted_tool_then_committed_summary(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "sk-secret-value"
    provider = ScriptedProvider(
        envelope=_final_envelope(f"Task synthesized; api_key={secret}")
    )
    _install_provider(monkeypatch, provider)
    runtime = _runtime(tmp_path, StubBroker(), ProviderConfig(model="fake-model"))
    observed: list[tuple[dict[str, object], str]] = []

    def capture(kind: str, payload: dict[str, object]) -> None:
        if kind == "output":
            controller = runtime.services.workflow.clarification
            assert controller is not None
            observed.append((payload, controller._store.load().status))

    runtime.subscribe(capture)
    try:
        draft = await runtime.task_clarification_start("predict churn")
        replayed = runtime.replay_output_events()
    finally:
        await runtime.aclose()

    assert draft.status == "READY_FOR_CONFIRMATION"
    assert provider.calls == 2
    assert provider.tool_names == [
        [REPORT_TASK_UNDERSTANDING_TOOL],
        [REPORT_TASK_UNDERSTANDING_TOOL],
    ]
    assert [status for _event, status in observed] == [
        "CLARIFYING",
        "READY_FOR_CONFIRMATION",
    ]
    events = [event for event, _status in observed]
    assert [(event["source"], event["tool"]) for event in events] == [
        ("tool", REPORT_TASK_UNDERSTANDING_TOOL),
        ("agent", None),
    ]
    assert all(
        (event["session_id"], event["scope"], event["scope_id"])
        == ("s1", TASK_UNDERSTANDING_SCOPE, draft.draft_id)
        for event in events
    )
    assert secret not in repr(events)
    assert all("[REDACTED]" in str(event["text"]) for event in events)
    assert replayed == [events[1]]


@pytest.mark.asyncio
async def test_llm_runtime_failure_is_retryable_fixed_and_replayed_once(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "sk-secret-value"
    provider = ScriptedProvider(error=f"provider down api_key={secret}")
    _install_provider(monkeypatch, provider)
    broker = StubBroker()
    runtime = _runtime(tmp_path, broker, ProviderConfig(model="fake-model"))
    outputs: list[dict[str, object]] = []
    runtime.subscribe(
        lambda kind, payload: outputs.append(payload) if kind == "output" else None
    )
    try:
        draft = await runtime.task_clarification_start("predict churn")
        replayed = runtime.replay_output_events()
    finally:
        await runtime.aclose()

    assert draft.status == "FAILED"
    assert draft.failure is not None and draft.failure.retryable is True
    assert broker.asked == []
    assert provider.calls == 1
    assert len(outputs) == 1
    assert outputs[0]["text"] == CLARIFICATION_FAILURE_NOTICE
    assert outputs[0]["channel"] == "error"
    assert outputs[0]["source"] == "agent"
    assert (
        outputs[0]["session_id"],
        outputs[0]["scope"],
        outputs[0]["scope_id"],
    ) == ("s1", TASK_UNDERSTANDING_SCOPE, draft.draft_id)
    assert secret not in repr(outputs)
    assert "provider down" not in repr(outputs)
    assert replayed == outputs
