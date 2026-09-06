"""``athena.memory.context_manager.ContextManager`` 的单元测试。"""

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    UserPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


def _tool_call(name: str, args: dict) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args)])


def _tool_result(name: str, content: str, call_id: str = "c1") -> ModelRequest:
    return ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name=name,
                content=content,
                tool_call_id=call_id,
            )
        ]
    )


class TestContextManager:

    def test_empty_context_has_zero_tokens(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        assert ctx.tokens == 0
        assert ctx.items == []
        assert ctx.version == 0

    def test_append_increments_tokens_and_version(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("hello world"))
        assert ctx.tokens > 0
        assert ctx.version == 1
        assert len(ctx.items) == 1

    def test_append_multiple_accrues_tokens(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("hello"))
        t1 = ctx.tokens
        ctx.append(_assistant("hi there"))
        assert ctx.tokens > t1
        assert ctx.version == 2

    def test_short_message_still_costs_tokens(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("x"))
        assert ctx.tokens > 0

    def test_items_returns_shallow_copy(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("a"))
        copy = ctx.items
        copy.clear()
        assert len(ctx.items) == 1

    def test_large_tool_result_is_truncated(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_tool_result("bash", "x" * 100_000))
        part = ctx.items[0].parts[0]
        c = getattr(part, "content", "")
        assert isinstance(c, str) and len(c) < 100_000
        assert "[TRUNCATED]" in c

    def test_truncation_preserves_head_and_tail_without_mutating_input(self):
        from athena.memory.context_manager import ContextManager

        original_content = "BEGIN" + ("x" * 60_000) + "END"
        msg = _tool_result("bash", original_content)
        ctx = ContextManager()
        ctx.append(msg)

        stored = getattr(ctx.items[0].parts[0], "content", "")
        assert stored.startswith("BEGIN")
        assert stored.endswith("END")
        assert len(stored) <= 50_000
        assert msg.parts[0].content == original_content

    def test_tool_result_under_limit_stays_intact(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_tool_result("bash", "short output"))
        c = getattr(ctx.items[0].parts[0], "content", "")
        assert c == "short output"

    def test_system_prompt_not_truncated(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        huge = "y" * 60_000
        ctx.append(ModelRequest(parts=[SystemPromptPart(content=huge)]))
        c = getattr(ctx.items[0].parts[0], "content", "")
        assert isinstance(c, str) and len(c) == 60_000

    def test_token_margin_positive_when_under_limit(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager(context_limit=200_000)
        assert ctx.token_margin() == int(200_000 * 0.85)

    def test_token_margin_decreases_after_appends(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager(context_limit=200_000)
        m1 = ctx.token_margin()
        ctx.append(_user("hello world " * 100))
        assert ctx.token_margin() < m1


class TestReplaceRange:

    def test_replace_keeps_token_count_accurate(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("hello world"))
        ctx.append(_assistant("hi back"))
        before = ctx.tokens
        ctx.replace_range(
            0, 2, [ModelRequest(parts=[SystemPromptPart(content="summary")])]
        )
        assert ctx.tokens < before
        assert len(ctx.items) == 1
        assert ctx.version == 3

    def test_replace_empty_range(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("a"))
        before = ctx.tokens
        v0 = ctx.version
        ctx.replace_range(0, 0, [])
        assert ctx.tokens == before
        assert ctx.version == v0 + 1

    def test_replace_partial_range_keeps_tail(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        for text in ("first", "second", "third"):
            ctx.append(_user(text))
        ctx.replace_range(0, 2, [_user("merged")])
        assert len(ctx.items) == 2
        tail = getattr(ctx.items[1].parts[0], "content", "")
        assert tail == "third"

    def test_version_monotonic_across_operations(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        v0 = ctx.version
        ctx.append(_user("a"))
        v1 = ctx.version
        ctx.replace_range(0, 1, [_assistant("b")])
        v2 = ctx.version
        assert v0 < v1 < v2

    def test_replacement_applies_same_tool_output_limit(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("old"))
        replacement = _tool_result("bash", "z" * 80_000)
        ctx.replace_range(0, 1, [replacement])
        stored = ctx.items[0].parts[0].content
        assert len(stored) <= 50_000
        assert "[TRUNCATED]" in stored
        assert replacement.parts[0].content == "z" * 80_000


class TestTokenEstimation:

    def test_english_text_estimate_is_plausible(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_user("hello world " * 36))  # 约 400 字符
        assert 80 <= ctx.tokens <= 120

    def test_code_block_estimate_is_plausible(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_assistant("def foo():\n    return 42\n" * 50))
        assert ctx.tokens > 0


class TestEdgeCases:

    def test_context_limit_is_stored(self):
        from athena.memory.context_manager import ContextManager

        assert ContextManager(context_limit=128_000).limit == 128_000

    def test_tool_call_arguments_contribute_tokens(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(_tool_call("bash", {"command": "ls"}))
        assert ctx.tokens > 0

    def test_mixed_parts_token_sum(self):
        from athena.memory.context_manager import ContextManager

        ctx = ContextManager()
        ctx.append(
            ModelResponse(
                parts=[
                    TextPart(content="Let me check."),
                    ToolCallPart(tool_name="bash", args={"command": "ls"}),
                ]
            )
        )
        assert ctx.tokens > 0  # 仅 TextPart 被计数
