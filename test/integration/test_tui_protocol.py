"""ResearchRuntime/TUI protocol integration tests."""

import asyncio
from pathlib import Path

import pytest

from athena.core.agent.provider import StreamEvent
from athena.research.runtime import ResearchRuntime
from athena_tui.controller import TuiController


class _AnswerProvider:
    model_name = "tui-protocol-test"

    async def stream(self, _config, _tools, _messages, _cancel, **_kwargs):
        yield StreamEvent(
            kind="text_delta",
            data={"delta": '{"answer":"ack"}', "accumulated": '{"answer":"ack"}'},
        )
        yield StreamEvent(kind="response_completed", data={"finish_reason": "stop"})


@pytest.mark.asyncio
async def test_runtime_publishes_only_output_and_state_and_reconnects_with_state_first(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    first: list[tuple[str, dict[str, object]]] = []
    second: list[tuple[str, dict[str, object]]] = []

    first_id = runtime.subscribe(lambda kind, data: first.append((kind, data)))
    await runtime.publish_output(source="agent", channel="text", text="new output")
    runtime.unsubscribe(first_id)
    runtime.subscribe(lambda kind, data: second.append((kind, data)))

    assert [kind for kind, _ in first] == ["state", "output"]
    assert [kind for kind, _ in second] == ["state"]
    assert {kind for kind, _ in first + second} <= {"output", "state"}
    await runtime.aclose()


@pytest.mark.asyncio
async def test_whitespace_only_agent_text_deltas_are_not_projected(tmp_path) -> None:
    """纯空白流式 delta（换行/缩进）不得产生空 ``* agent`` 显示事件。"""
    runtime = ResearchRuntime(project_root=tmp_path)
    seen: list[tuple[str, dict[str, object]]] = []
    runtime.subscribe(lambda kind, data: seen.append((kind, data)))

    await runtime.events.project_agent_event(
        "prepare", "agent/text_delta", "event:1", {"delta": "\n", "accumulated": "\n"}
    )
    await runtime.events.project_agent_event(
        "prepare", "agent/text_delta", "event:2", {"delta": "  ", "accumulated": "  \n"}
    )
    await runtime.events.project_agent_event(
        "prepare",
        "agent/text_delta",
        "event:3",
        {"delta": "answer", "accumulated": "\nanswer"},
    )

    outputs = [payload for kind, payload in seen if kind == "output"]
    assert [payload["text"] for payload in outputs] == ["answer"]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_message_publishes_final_supervisor_answer(tmp_path) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    runtime.register_supervisor(provider=_AnswerProvider())
    seen: list[tuple[str, dict[str, object]]] = []
    runtime.subscribe(lambda kind, data: seen.append((kind, data)))

    answer = await runtime.message("continue research")

    assert answer == "ack"
    assert [
        (payload["source"], payload["channel"], payload["text"])
        for kind, payload in seen
        if kind == "output"
    ] == [("supervisor", "text", "ack")]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_async_subscriber_finishes_initial_state_before_new_output(
    tmp_path,
) -> None:
    runtime = ResearchRuntime(project_root=tmp_path)
    release_state = asyncio.Event()
    seen: list[str] = []

    async def receive(kind: str, _payload: dict[str, object]) -> None:
        if kind == "state":
            await release_state.wait()
        seen.append(kind)

    runtime.subscribe(receive)
    publishing = asyncio.create_task(
        runtime.publish_output(source="agent", channel="text", text="new")
    )
    await asyncio.sleep(0)
    assert seen == []
    release_state.set()
    await publishing
    assert seen == ["state", "output"]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_tui_protocol_has_no_dispatch_or_filesystem_dependency(
    monkeypatch, tmp_path
) -> None:
    class Runtime:
        def __init__(self) -> None:
            self.callback = None
            self.messages: list[str] = []

        def subscribe(self, callback) -> str:
            self.callback = callback
            callback(
                "state",
                {
                    "type": "state",
                    "status": "RUNNING",
                    "phase": "SEARCH",
                    "plans": [],
                    "search": {
                        "attempts": 0,
                        "limit": 10,
                        "successes": 0,
                        "concurrency": 4,
                    },
                    "sota": None,
                    "waiting": None,
                },
            )
            return "sub"

        def unsubscribe(self, _subscription_id: str) -> None:
            self.callback = None

        async def message(self, text: str) -> str:
            self.messages.append(text)
            return "ok"

        async def aclose(self) -> None:
            pass

        def dispatch(self, *_args, **_kwargs):
            raise AssertionError("TUI must not call dispatch")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("TUI must not read .athena sessions")

    monkeypatch.setattr(Path, "read_text", forbidden)
    runtime = Runtime()
    controller = TuiController(runtime, emit=lambda _event: None)
    await controller.connect()
    await controller.send_message("continue research")

    assert runtime.messages == ["continue research"]
    await controller.aclose()
