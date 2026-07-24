"""Unit tests for Codex-style local context compaction."""

from types import SimpleNamespace

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


class _FakeMessages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(text="compact summary")])


class _FakeLLM:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


class TestCompactor:
    def test_should_compact_uses_configured_threshold(self):
        from athena.memory import Compactor, ContextManager

        ctx = ContextManager()
        ctx.append(_user("x" * 100))
        compactor = Compactor()
        assert compactor.should_compact(ctx, at_tokens=ctx.tokens)
        assert not compactor.should_compact(ctx, at_tokens=ctx.tokens + 1)

    async def test_compact_replaces_old_history_without_duplicating_tail(self):
        from athena.memory import Compactor, ContextManager

        ctx = ContextManager()
        ctx.append(_user("first " * 100))
        ctx.append(_assistant("second " * 100))
        tail = _user("recent")
        ctx.append(tail)
        before_version = ctx.version
        llm = _FakeLLM()

        checkpoint = await Compactor(keep_recent=1).compact(ctx, llm)

        assert checkpoint.summary == "compact summary"
        assert len(checkpoint.original_items) == 2
        assert checkpoint.original_items[0].parts[0].content.startswith("first")
        assert checkpoint.original_items[1].parts[0].content.startswith("second")
        assert len(ctx.items) == 2
        assert ctx.items[-1].parts[0].content == tail.parts[0].content
        assert (
            sum(
                1
                for item in ctx.items
                if item.parts[0].content == tail.parts[0].content
            )
            == 1
        )
        assert ctx.version == before_version + 1
        prompt = llm.messages.calls[0]["messages"][0]["content"]
        assert "first" in prompt
        assert "second" in prompt
        assert "recent" not in prompt

    async def test_compact_with_nothing_old_is_a_noop(self):
        from athena.memory import Compactor, ContextManager

        ctx = ContextManager()
        only = _user("only")
        ctx.append(only)
        llm = _FakeLLM()
        checkpoint = await Compactor(keep_recent=10_000).compact(ctx, llm)

        assert checkpoint.original_items == []
        assert checkpoint.summary == ""
        assert ctx.items == [only]
        assert llm.messages.calls == []
