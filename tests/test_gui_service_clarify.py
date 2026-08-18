"""parse_intent 多轮选择题澄清的单元测试。"""

from pathlib import Path

import pytest

import athena.gui.service as svc
from athena.gui.service import GuiService


class ClarifyRuntime:
    def __init__(self) -> None:
        self.tree_path = Path("/tmp/athena-runtime")
        self.persisted: list[str] = []
        self.artifacts: dict[str, str] = {}

        class Store:
            def __init__(self, owner) -> None:
                self.owner = owner

            async def put_text(self, text: str) -> str:
                ref = f"sha256:{'a' * 64}"
                self.owner.artifacts[ref] = text
                return ref

        self._store = Store(self)
        self._state_path = Path("/tmp/state.json")

        class State:
            def __init__(self) -> None:
                self.handoff_refs = {}

            def save(self, _path) -> None:
                pass

        self.state = State()

    def persist_user_message(self, text: str) -> None:
        self.persisted.append(text)

    def replay_output_events(self) -> list[dict]:
        return []


class FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})()


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.messages: list[list[dict]] = []

    async def create(self, **kwargs):
        self.messages.append(kwargs["messages"])
        return FakeResponse(self.payloads.pop(0))


class FakeClient:
    def __init__(self, payloads: list[dict]) -> None:
        self.chat = type("Chat", (), {"completions": FakeCompletions(payloads)})()


class FakeBroker:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.asked: list[tuple[str, list[dict] | None]] = []

    async def ask(self, prompt, *, choices=None, allow_custom=True, allow_skip=True):
        self.asked.append((prompt, choices))
        return self.answers.pop(0) if self.answers else "skip"


@pytest.mark.asyncio
async def test_parse_intent_clarifies_then_writes_handoff(monkeypatch) -> None:
    runtime = ClarifyRuntime()
    broker = FakeBroker(["choice:f1"])
    service = GuiService(runtime, broker=broker)

    payloads = [
        '{"done": false, "question": "choose metric", "choices": [{"label": "f1", "value": "f1"}]}',
        '{"done": true, "understanding": {"title": "t", "dataset": "d", "target": "y", "task_type": "classification", "primary_metric": "f1", "direction": "maximize", "evaluation_plan": "holdout", "needs_configuration": false}}',
    ]
    client = FakeClient(payloads)
    monkeypatch.setattr(svc.settings, "get_client", lambda: client)
    monkeypatch.setattr(svc.settings, "model_name", lambda: "m")

    result = await service.parse_intent("ambiguous task")

    assert result["primary_metric"] == "f1"
    assert broker.asked == [
        ("choose metric", [{"label": "f1", "value": "f1"}])
    ]
    assert runtime.state.handoff_refs.get("task_clarification")
    clarification = runtime.artifacts[runtime.state.handoff_refs["task_clarification"]]
    assert "choose metric" in clarification
    assert "choice:f1" in clarification
